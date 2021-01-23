from contextlib import ExitStack
from math import inf
from trio import fail_after, open_nursery, sleep_forever, TooSlowError
from trio_util import periodic
from typing import Any, Dict

from flockwave.concurrency import CancellableTaskGroup
from flockwave.ext.base import ExtensionBase
from flockwave.server.tasks import wait_for_dict_items, wait_until

from .clock import ShowClock
from .config import DroneShowConfiguration, LightConfiguration, StartMethod

__all__ = ("construct", "dependencies")


class DroneShowExtension(ExtensionBase):
    """Extension that prepares the server to be able to manage drone shows.

    The extension provides three signals via the `signals` extension; `show:start`
    is emitted when the show starts, `show:config_updated` is emitted when
    the configuration of the show startup changes, and `show:lights_updated` is
    emitted when the configuration of the LED lights on the drones changes. The
    latter two receive a keyword argument named `config` that contains the new
    configuration.
    """

    def __init__(self):
        super().__init__()

        self._clock = None
        self._nursery = None
        self._show_tasks = None

        self._config = DroneShowConfiguration()
        self._lights = LightConfiguration()

    def exports(self) -> Dict[str, Any]:
        return {
            "get_configuration": self._get_configuration,
            "get_light_configuration": self._get_light_configuration,
        }

    def handle_SHOW_CFG(self, message, sender, hub):
        return hub.create_response_or_notification(
            body={"configuration": self._config.json}, in_response_to=message
        )

    def handle_SHOW_LIGHTS(self, message, sender, hub):
        return hub.create_response_or_notification(
            body={"configuration": self._lights.json}, in_response_to=message
        )

    def handle_SHOW_SETCFG(self, message, sender, hub):
        try:
            self._config.update_from_json(message.body.get("configuration", {}))
            return hub.acknowledge(message)
        except Exception as ex:
            return hub.acknowledge(message, outcome=False, reason=str(ex))

    def handle_SHOW_SETLIGHTS(self, message, sender, hub):
        try:
            self._lights.update_from_json(message.body.get("configuration", {}))
            return hub.acknowledge(message)
        except Exception as ex:
            return hub.acknowledge(message, outcome=False, reason=str(ex))

    async def run(self, app, configuration, logger):
        self._clock = ShowClock()
        handlers = {
            "SHOW-CFG": self.handle_SHOW_CFG,
            "SHOW-LIGHTS": self.handle_SHOW_LIGHTS,
            "SHOW-SETCFG": self.handle_SHOW_SETCFG,
            "SHOW-SETLIGHTS": self.handle_SHOW_SETLIGHTS,
        }

        async with open_nursery() as self._nursery:
            self._show_tasks = CancellableTaskGroup(self._nursery)

            with ExitStack() as stack:
                stack.enter_context(
                    self._config.updated.connected_to(
                        self._on_config_updated, sender=self._config
                    )
                )
                stack.enter_context(
                    self._lights.updated.connected_to(
                        self._on_lights_updated, sender=self._lights
                    )
                )
                stack.enter_context(app.import_api("clocks").use_clock(self._clock))
                stack.enter_context(app.message_hub.use_message_handlers(handlers))
                await sleep_forever()

    def _get_configuration(self) -> DroneShowConfiguration:
        """Returns a copy of the current drone show configuration."""
        return self._config.clone()

    def _get_light_configuration(self) -> LightConfiguration:
        """Returns a copy of the current LED lgiht configuration."""
        return self._lights.clone()

    def _on_config_updated(self, sender):
        """Handler that is called when the configuration of the start settings
        of the show was updated from any source.
        """
        self._clock.start_time = self._config.start_time

        self._show_tasks.cancel_all()

        if self._should_run_countdown:
            self._show_tasks.start_soon(self._start_show_when_needed)
            self._show_tasks.start_soon(self._manage_countdown_before_start)

        updated_signal = self.app.import_api("signals").get("show:config_updated")
        updated_signal.send(self, config=self._config.clone())

    def _on_lights_updated(self, sender):
        """Handler that is called when the configuration of the LED lights was
        updated from any source.
        """
        updated_signal = self.app.import_api("signals").get("show:lights_updated")
        updated_signal.send(self, config=self._lights.clone())

    @property
    def _should_run_countdown(self) -> bool:
        """Returns whether the extension should run the clock countdown, given
        its current configuration.
        """
        return (
            self._config.authorized_to_start
            and self._clock.start_time is not None
            and self._config.start_method is StartMethod.AUTO
        )

    async def _start_show_when_needed(self):
        start_signal = self.app.import_api("signals").get("show:start")

        await wait_until(self._clock, seconds=0, edge_triggered=True)

        self._start_uavs_if_needed()
        start_signal.send(self)

        delay = int(self._clock.seconds * 1000)
        if delay >= 1:
            self.log.warn(f"Started show with a delay of {delay} ms")
        else:
            self.log.info("Started show accurately")

    async def _manage_countdown_before_start(self):
        await wait_until(self._clock, seconds=-11, edge_triggered=False)

        last_seconds = -inf
        try:
            async for _ in periodic(1):
                seconds = self._clock.seconds
                if not self._should_run_countdown:
                    break
                elif last_seconds > seconds:
                    self._notify_uavs_about_countdown_state(cancelled=True)
                elif seconds > -0.5:
                    break
                else:
                    self._notify_uavs_about_countdown_state(seconds_left=-seconds)
                    last_seconds = seconds
        finally:
            # Cancel any countdowns that we may have started if the clock was
            # stopped or the authorization was revoked
            if not self._should_run_countdown:
                self._notify_uavs_about_countdown_state(cancelled=True)

    def _notify_uavs_about_countdown_state(
        self, seconds_left: float = 0, cancelled: bool = False
    ):
        delay = seconds_left if not cancelled else None
        uavs_by_drivers = self.app.sort_uavs_by_drivers(self._config.uav_ids)
        for driver, uavs in uavs_by_drivers.items():
            results = driver.send_takeoff_countdown_notification(uavs, delay)
            self._nursery.start_soon(
                self._process_command_results_in_background,
                results,
                "countdown notifications",
            )

    def _start_uavs_if_needed(self):
        uavs_by_drivers = self.app.sort_uavs_by_drivers(self._config.uav_ids)
        for driver, uavs in uavs_by_drivers.items():
            results = driver.send_takeoff_signal(uavs, scheduled=True)
            self._nursery.start_soon(
                self._process_command_results_in_background, results, "start signals"
            )

    async def _process_command_results_in_background(
        self, results, what: str = "commands"
    ):
        try:
            with fail_after(5):
                results = await wait_for_dict_items(results)
        except TooSlowError:
            self.log.warn(f"Failed to send {what} to {len(results)} UAVs in 5 seconds")
            return

        failed = [key for key, value in results.items() if isinstance(value, Exception)]
        if failed:
            failed = ", ".join([getattr(uav, "id", "-no-id-") for uav in failed])
            self.log.warn(f"Failed to send {what} to {failed}")


construct = DroneShowExtension
dependencies = ("clocks", "signals")
