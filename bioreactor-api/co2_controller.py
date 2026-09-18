"""API adapter for the driver's shared CO2 MPC worker."""
import json
import logging
import time
from bioreactor_v3.src.co2_control import CO2Control

logger = logging.getLogger(__name__)


class CO2API:
    def __init__(self, config, gas_sampler, relays, components):
        self.relays = relays
        self.config = config
        self.components = components
        self.guard = getattr(config, 'RELAY_SAFETY', {}).get('CO2', {})
        def valve(on):
            if not components.get('relays') or 'CO2' not in relays._names:
                if on:
                    raise RuntimeError('CO2 relay is unavailable')
                return
            relays._set('CO2', on)
            if on:
                relays._last_dose['CO2'] = time.time()
        self.worker = CO2Control(lambda: gas_sampler.sample('co2'),
            valve, getattr(config, 'CO2_MPC', None),
            log=lambda row: logger.info('CO2 MPC %s', json.dumps(row, allow_nan=False)))
        # The API may have restarted with gas still in transit from a prior dose.
        self.worker._last_stop = time.monotonic()

    def enable_history(self):
        from bioreactor_v3.src.co2_history import hardware_identity, invalidate_unconfigured
        path = getattr(self.config, 'CO2_MPC_STATE_PATH', None)
        if path and self.worker.profile:
            self.worker.enable_history(path, hardware_identity(self.config))
        elif path:
            invalidate_unconfigured(path)

    def _confirm_manual_closure(self):
        history = self.worker.history
        if history and history.state['pending'] == 'external':
            state = self.relays.status()
            if state['states'].get('CO2') == 'open' and not state['pending'].get('CO2'):
                try:
                    self.worker.external_closed()
                except RuntimeError as e:
                    self.worker.fault = str(e)

    def validate(self, target, duration_s=None):
        if not self.components.get('co2_sensor') or not self.components.get('relays'):
            raise ValueError('CO2 sensor and relays must be initialized')
        if 'CO2' not in self.relays._names:
            raise ValueError('config.RELAYS must contain CO2')
        model, settings = self.worker.validate(target)
        self.worker.validate_duration(duration_s)
        if not self.guard or self.guard.get('co2_max_ppm') is None:
            raise ValueError('CO2 RELAY_SAFETY guard is required')
        if settings.max_ppm > min(95000, self.guard['co2_max_ppm']):
            raise ValueError('MPC ceiling exceeds CO2 relay ceiling / 95000 ppm')
        if settings.max_pulse_s > self.guard['max_duration_s']:
            raise ValueError('MPC maximum pulse exceeds CO2 relay guard')
        if settings.min_interval_s < self.guard['min_interval_s']:
            raise ValueError('MPC pulse interval is shorter than CO2 relay guard')
        return model, settings

    def start(self, target, owner='api', duration_s=None):
        self.validate(target, duration_s)
        with self.worker.lock:
            self._confirm_manual_closure()
            if not self.worker.active:
                state = self.relays.status()
                if state['states']['CO2'] != 'open' or state['pending'].get('CO2'):
                    raise RuntimeError('CO2 relay has a pending/active manual dose')
            self.worker.start(target, owner, duration_s)

    def manual(self, name, command, duration=None):
        """Serialize manual CO2 writes with autonomous ownership. OFF always wins."""
        with self.worker.lock:
            if name == 'CO2':
                timed_off = command == 'open' or (command == 'toggle' and
                            self.relays.states().get('CO2') == 'closed')
                if timed_off and duration is not None:
                    raise ValueError('Timed OFF would reopen CO2 later; use an immediate OFF command')
                if self.worker.active and command != 'open':
                    raise RuntimeError('CO2 MPC owns the valve; stop it before manual dosing')
                if command == 'open':
                    self.worker.stop(join=False)
                else:
                    self.worker.external_dose()
            return (self.relays.apply(name, command) if duration is None
                    else self.relays.timed(name, command, duration))

    def program(self, target, duration_s=None):
        if target is False:
            self.worker.stop(owner='program')
        else:
            self.start(target, owner='program', duration_s=duration_s)

    def status(self):
        with self.worker.lock:
            self._confirm_manual_closure()
        status = self.worker.status()
        profile = self.worker.profile or {}
        settings = profile.get('settings', {})
        trial = profile.get('trial', {}) if status['trial_mode'] else {}
        status.update(
            accepts_target_percent=True,
            target_percent=status['target_ppm'] / 10000 if status['target_ppm'] is not None else None,
            limits={
                'trial_target_max_percent': trial.get('target_max_ppm', 0) / 10000 if trial else None,
                'max_duration_s': min(604800, trial.get('max_duration_s', 604800)),
                'allow_indefinite': status['configured'] and self.worker.allows_indefinite(),
                'target_below_percent': (settings.get('max_ppm', 95000) - settings.get('margin_ppm', 5000)) / 10000,
            },
        )
        return status

    def stop(self, owner=None):
        if owner is None or self.worker.owner == owner:
            self.relays._cancel_timer('CO2')
        self.worker.stop(owner)
