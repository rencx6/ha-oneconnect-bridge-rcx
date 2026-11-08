from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store

from .business.models import BindingField, ChildPlan


_LOGGER = logging.getLogger(__name__)

# Characteristics whose wire type is int; HA states are always strings and must
# be converted before dataSync, otherwise the cloud silently rejects the shape.
_INT_CHARACTERISTICS = frozenset({
    "currentHumidity", "currentTemperature", "targetTemperature",
    "targetHumidity", "powerGear", "status", "code", "progress", "bootTime",
    # 04E 温湿度传感器 profile 里 temperature.current / humidity.current
    # 都是 int(method=R)，而 HA sensor state 恒为字符串 ("24.8")；不转 int
    # 时云端会静默丢弃该条 dataSync，卡片停在初始值。
    # 同名的 opener.current（窗帘开合度）也会由 float 变为 int，无副作用。
    "current",
    # 027 电热水器 A3L4 的 temperature.target(int RW)：HA number state 是
    # 字符串（"45"），不转 int 云端会丢弃 dataSync，App 列表温度停留在初始值。
    # 窗帘 opener.target 本就是 int，int() 为幂等操作，无副作用。
    "target",
})

# Characteristics whose wire type is float. Keyed by (sid, field_name), NOT by
# field name alone: A2OP has heat.current(float) alongside
# temperature.current(int) / humidity.current(int) with an identical field name.
# Checked BEFORE _INT_CHARACTERISTICS in _read_binding_value().
_FLOAT_CHARACTERISTICS = frozenset({
    ("heat", "current"),       # A2OP 卡片「当前温度」step 0.1，保留 1 位小数
    # A3ZZ 海尔冰箱：heat 服务挂在 refrigeratorFloat / freezerFloat 两个 sid 下，
    # current+target 均为 float（App 显示 5.0℃ / -18.0℃）。
    ("refrigeratorFloat", "current"),
    ("refrigeratorFloat", "target"),
    ("freezerFloat", "current"),
    ("freezerFloat", "target"),
})

# Last commanded cover target position (0-100) per entity_id. HA cover
# entities (e.g. xiaomi_home) only report current_position when the movement
# finishes, so the bridge interpolates toward this target while moving.
_COVER_TARGETS: dict[str, int] = {}

# Xiaomi Home / MIoT real-position probe (LOCAL LAN, not cloud shadow).
# DuYa YS7 spec (urn:miot-spec-v2:device:curtain:0000A00C:dooya-ys7:1):
#   service siid=2 (curtain), property piid=3 (current-position),
#   access=["read", "notify"], unit=percentage, range 0..100.
#
# MIoTClient.get_prop_async() deliberately reads the CLOUD SHADOW first (see
# miot_client.py: "obtaining the cache from the cloud is the priority here"),
# and the motor only notifies current-position on arrival -> that value is
# frozen while travelling.  The same client also owns a MIoTLan instance that
# talks miIO directly to the motor on UDP 54321.  Verified 2026-11-08 by LAN
# probe: did=873865751 -> 192.168.2.12, keep-alive carries MSUB/PUB (local
# subscription capable).  We therefore call miot_lan.get_prop_async directly.
_XIAOMI_POSITION_PROP = (2, 3)
_XIAOMI_PROBE_CACHE: dict[str, dict[str, Any] | None] = {}
# 2 = stopped/idle, 0 = closing, 1 = opening.  Only set while the estimator
# owns the entity; the mapping reports 2 for every idle HA state.
_XIAOMI_LAN_TIMEOUT_MS = 900

# Motion commands are destructive: they must arrive under the exact sid they
# were declared under.  The field-name fallback below is only allowed for
# state-like characteristics (on / brightness / ...).  See the 2026-11-08
# timer-payload incident in _async_apply_child_command.
_STRICT_SID_FIELDS = frozenset({"action", "target"})
# A1EI 音乐主机：瞬时动作 sid，下发后需两段式上报（先 1 对账、再 0 完成）
_MOMENTARY_ACTION_SIDS = frozenset({"commonExecution1", "commonExecution2"})

# ---------------------------------------------------------------------------
# Adaptive travel-time calibration
#
# Previously every motion was animated over a hard-coded 9.0 s full travel
# with 0.75 s frames (in practice ~0.92 s because the LAN read was added on
# top of each sleep).  Measured 2026-11-08 logs: a real 0<->100 DuYa YS7
# travel takes ~6.1-6.5 s, so the animation lagged behind the motor.
#
# The bridge now timestamps (a) the moment the button/command arrives and
# (b) the FIRST moment the motor reports its final position (LAN property
# read or HA arrival event).  The measured duration is normalised to a full
# 0<->100 travel and folded into a clamped EWMA, so the animation length
# converges on the real mechanical travel time.  Samples are persisted in HA
# .storage so a restart does not lose the calibration.
# ---------------------------------------------------------------------------
_DEFAULT_FULL_TRAVEL_SECONDS = 7.0   # prior from 2026-11-08 measurements
_MIN_FULL_TRAVEL_SECONDS = 2.5
_MAX_FULL_TRAVEL_SECONDS = 25.0
_LEARN_RATE = 0.55                  # weight of one new sample in the EWMA
_LEARN_OUTLIER_FACTOR = 2.2         # reject samples beyond old*2.2 / old/2.2
_TICK_INTERVAL = 0.40               # progress push cadence (was 0.75 s)
_MIN_MOTION_SECONDS = 1.2
# 2026-11-08: raised 3.0 -> 6.0.  The living-room YS7 (did 874074837) reports
# its final position only ~10.8 s after the tap, i.e. beyond the previous
# 7.0 s + 3.0 s window, so its motion never calibrated.  7 + 6 = 13 s covers
# the slowest motor while the loop still exits the moment truth arrives.
_ARRIVAL_GRACE_SECONDS = 6.0        # stay "running" while waiting for truth


class HAEntityBridge:
    """Read/call HA entities from the gateway's non-event-loop worker."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._motion_tasks: dict[str, asyncio.Task] = {}
        self._motion_start: dict[str, int] = {}
        self._motion_action: dict[str, int] = {}
        # Per-motion timing context: t0 (button press), start/target/distance,
        # and the animation duration that was planned from the learned model.
        self._motion_ctx: dict[str, dict[str, Any]] = {}
        self._last_push_pos: dict[str, int] = {}
        self._settle_guard_until: dict[str, float] = {}
        self._settle_handles: dict[str, Any] = {}
        self._state_callback = None
        # Cover entity ids whose service call was issued BY this bridge.  The
        # HA call_service listener must ignore those, otherwise one App tap
        # would start two estimators (one from the event, one from the cloud
        # command handler) and the second would cancel the first.
        self._bridge_command_active: set[str] = set()
        # Learned full-travel seconds per cover entity, persisted across HA
        # restarts.  HAEntityBridge is constructed inside async_setup_entry,
        # i.e. on the event loop, so create_task is safe here.
        self._travel_seconds: dict[str, float] = {}
        self._calibration_loaded = False
        self._calibration = Store(hass, 1, "oneconnect_bridge.cover_travel")

    async def _async_load_calibration(self) -> None:
        try:
            data = await self._calibration.async_load() or {}
        except Exception as err:
            _LOGGER.warning("Cover travel calibration load failed: %s", err)
            return
        saved = data.get("travel_seconds") if isinstance(data, dict) else None
        if isinstance(saved, dict):
            for eid, value in saved.items():
                if (
                    isinstance(eid, str)
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and _MIN_FULL_TRAVEL_SECONDS <= value <= _MAX_FULL_TRAVEL_SECONDS
                ):
                    self._travel_seconds.setdefault(eid, float(value))
        _LOGGER.info(
            "Cover travel calibration loaded: %s",
            {k: round(v, 2) for k, v in self._travel_seconds.items()},
        )

    async def _async_save_calibration(self) -> None:
        try:
            await self._calibration.async_save({
                "travel_seconds": {
                    key: round(value, 3)
                    for key, value in self._travel_seconds.items()
                },
            })
        except Exception as err:
            _LOGGER.warning("Cover travel calibration save failed: %s", err)

    def _learn_travel_time(self, entity_id: str, source: str,
                           arrived_at: float | None = None) -> None:
        """Fold one measured click -> final-state duration into the model."""
        ctx = self._motion_ctx.pop(entity_id, None)
        if ctx is None:
            return
        elapsed = (arrived_at or time.monotonic()) - ctx["t0"]
        if source == "LAN":
            # LAN truth is sampled once per animation frame, so the real
            # arrival lies somewhere inside the last frame.  Use its midpoint
            # to avoid a systematic +half-frame bias in the learned duration.
            elapsed = max(0.0, elapsed - _TICK_INTERVAL / 2.0)
        distance = ctx["distance"]
        old = self._travel_seconds.get(entity_id, _DEFAULT_FULL_TRAVEL_SECONDS)
        # A pause/stop, a superseded command or a flicker event must never
        # teach the model a bogus (usually far too short) travel time.
        if elapsed < 1.0 or distance < 20 or elapsed < ctx["est"] * 0.35:
            _LOGGER.debug(
                "Travel sample rejected (implausible) entity=%s source=%s "
                "elapsed=%.2fs distance=%s",
                entity_id, source, elapsed, distance,
            )
            return
        measured = elapsed * 100.0 / distance
        if not (_MIN_FULL_TRAVEL_SECONDS <= measured <= _MAX_FULL_TRAVEL_SECONDS):
            _LOGGER.debug(
                "Travel sample rejected (out of range) entity=%s source=%s "
                "full_equiv=%.2fs", entity_id, source, measured,
            )
            return
        if (
            measured > old * _LEARN_OUTLIER_FACTOR
            or measured < old / _LEARN_OUTLIER_FACTOR
        ):
            _LOGGER.debug(
                "Travel sample rejected (outlier) entity=%s source=%s "
                "full_equiv=%.2fs previous=%.2fs",
                entity_id, source, measured, old,
            )
            return
        new = old + (measured - old) * _LEARN_RATE
        new = max(_MIN_FULL_TRAVEL_SECONDS,
                  min(_MAX_FULL_TRAVEL_SECONDS, new))
        self._travel_seconds[entity_id] = new
        # Block stale post-arrival HA events from the moment of arrival, not
        # from the (later) settled push.
        self._settle_guard_until[entity_id] = time.monotonic() + 3.0
        _LOGGER.info(
            "Cover travel learned entity=%s source=%s click_to_arrival=%.2fs "
            "distance=%s full_equiv=%.2fs previous=%.2fs -> now=%.2fs "
            "(next full animation %.2fs, tick %.2fs)",
            entity_id, source, elapsed, distance, measured, old, new,
            max(new, _MIN_MOTION_SECONDS), _TICK_INTERVAL,
        )
        try:
            asyncio.get_running_loop().create_task(self._async_save_calibration())
        except RuntimeError:
            pass

    def _cover_entity(self, child: ChildPlan) -> str | None:
        for binding in child.bindings:
            if binding.entity_id and binding.entity_id.startswith("cover."):
                return binding.entity_id
        return None

    def _inject_motion_state(
        self, child: ChildPlan, snapshot: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        """While travelling, report the Huawei motor mode (0/1), not idle (2).

        The official H5 CurtainAll SDK (haspause=true) renders its banner text
        purely from action.action: 0 -> "关闭中", 1 -> "打开中", 2 -> "已关闭/
        已打开" (based on currentValue).  It also uses 0/1 to highlight the
        left/right button, which is what turns the tap into a pause.  So every
        snapshot emitted during a motion must carry the running action and
        must NOT carry opener.target (translate() prefers target over current).
        """
        entity_id = self._cover_entity(child)
        action = self._motion_action.get(entity_id) if entity_id else None
        if action is None:
            return snapshot
        snapshot.setdefault("action", {})["action"] = action
        snapshot.setdefault("opener", {}).pop("target", None)
        return snapshot

    async def _async_push_state(
        self, child: ChildPlan, entity_id: str | None = None,
        position: int | None = None,
    ) -> dict[str, dict[str, object]]:
        """Build, decorate and forward one child snapshot."""
        snapshot = await self._async_read_child_state(child)
        if position is not None:
            opener = snapshot.setdefault("opener", {})
            # At rest H5 translate() prefers target, so keep both fields in
            # sync; while moving _inject_motion_state() drops target again.
            opener["current"] = position
            opener["target"] = position
        self._inject_motion_state(child, snapshot)
        if self._state_callback is not None:
            current = snapshot.get("opener", {}).get("current")
            if entity_id and isinstance(current, (int, float)):
                self._last_push_pos[entity_id] = int(current)
            self._state_callback(child.key, snapshot)
        return snapshot

    async def _async_push_settled(
        self, child: ChildPlan, entity_id: str,
        fallback: int | None = None, expect: int | None = None,
    ) -> dict[str, dict[str, object]]:
        """Push the final idle state using the LAN truth, not HA's stale value."""
        # Force idle regardless of any lingering estimator state: the settled
        # snapshot MUST carry action=2 or the card stays on "关闭中/打开中".
        self._motion_tasks.pop(entity_id, None)
        self._motion_start.pop(entity_id, None)
        self._motion_action.pop(entity_id, None)
        self._motion_ctx.pop(entity_id, None)
        self._settle_guard_until[entity_id] = time.monotonic() + 3.0
        pos = await self._async_read_real_cover_position(entity_id)
        if pos is None:
            pos = fallback
        # After a full travel the commanded endpoint IS the true position;
        # a lagging/frozen LAN answer must not leave the card at the old value.
        if expect is not None and pos is not None and abs(pos - expect) > 3:
            pos = expect
        return await self._async_push_state(child, entity_id, position=pos)

    def read_child_state(self, child: ChildPlan) -> dict[str, dict[str, object]]:
        result = self._inject_motion_state(
            child, self._run(self._async_read_child_state(child))
        )
        _LOGGER.debug("HA state snapshot child=%s state=%s", child.key, result)
        return result

    def apply_child_command(
        self, child: ChildPlan, service_id: str, payload: dict[str, object]
    ) -> None:
        _LOGGER.debug(
            "HA command received child=%s sid=%s payload=%s",
            child.key,
            service_id,
            payload,
        )
        self._run(self._async_apply_child_command(child, service_id, payload))

    def setup_state_listener(self, plan, callback):
        """Forward bound HA state changes to the gateway worker."""

        # This method runs on the HA event loop, so loading the persisted
        # calibration here is safe (and is done only once per bridge).
        if not self._calibration_loaded:
            self._calibration_loaded = True
            asyncio.create_task(self._async_load_calibration())

        entity_ids = {
            binding.entity_id
            for child in plan.children
            for binding in child.bindings
            if binding.entity_id
        }
        if not entity_ids:
            return lambda: None
        self._state_callback = callback

        async def _probe_startup(_now=None) -> None:
            """One-shot check of both LAN and cloud position channels."""
            for eid in entity_ids:
                if not eid.startswith("cover."):
                    continue
                lan = await self._async_read_real_cover_position(eid)
                cloud = await self._async_read_cloud_shadow(eid)
                full = self._travel_seconds.get(
                    eid, _DEFAULT_FULL_TRAVEL_SECONDS
                )
                _LOGGER.debug(
                    "Cover position channels entity=%s LAN=%s cloud_shadow=%s "
                    "learned_full_travel=%.2fs tick=%.2fs",
                    eid, lan, cloud, full, _TICK_INTERVAL,
                )

        async_call_later(self._hass, 6.0, _probe_startup)

        async def state_changed(event) -> None:
            entity_id = event.data.get("entity_id")
            for child in plan.children:
                if not any(binding.entity_id == entity_id for binding in child.bindings):
                    continue
                new_state = event.data.get("new_state")
                new_val = new_state.state if new_state else None
                pos = (
                    new_state.attributes.get("current_position")
                    if new_state else None
                )
                target_pos = _COVER_TARGETS.get(entity_id)
                running = entity_id in self._motion_tasks

                # xiaomi_home emits one last event with the OLD pre-movement
                # current_position ~100 ms after arrival.  Suppress it during
                # the short settle window, otherwise the card flashes back to
                # the previous position right after we pushed the truth.
                if (
                    not running and pos is not None
                    and time.monotonic() < self._settle_guard_until.get(entity_id, 0)
                    and entity_id in self._last_push_pos
                    and abs(int(pos) - self._last_push_pos[entity_id]) > 20
                ):
                    _LOGGER.debug(
                        "Suppressing stale post-motion HA event child=%s "
                        "entity=%s state=%s pos=%s last=%s",
                        child.key, entity_id, new_val, pos,
                        self._last_push_pos[entity_id],
                    )
                    continue

                # During travel the LAN-backed estimator owns the progress
                # bar.  xiaomi_home fires several events in that window whose
                # current_position is still the pre-movement (frozen) value;
                # forwarding them would make the animation jump backwards and
                # would also flip the motor mode back to idle.  Only the
                # arrival event (position == commanded target) is meaningful.
                if running:
                    if (
                        pos is not None and target_pos is not None
                        and int(pos) == int(target_pos)
                    ):
                        # The motor (via the cloud subscription) just reported
                        # its final state: this is the arrival timestamp used
                        # to calibrate the animation length.  Capture the
                        # context before _cancel_motion discards it.
                        ctx = self._motion_ctx.get(entity_id) or {}
                        self._learn_travel_time(entity_id, "HA arrival event")
                        self._cancel_motion(entity_id)
                        self._schedule_settle(
                            child, entity_id,
                            expect=ctx.get("target"),
                            fallback=ctx.get("target"),
                        )
                    else:
                        _LOGGER.debug(
                            "Suppressing mid-motion HA event child=%s "
                            "entity=%s state=%s pos=%s",
                            child.key, entity_id, new_val, pos,
                        )
                        continue
                elif new_val in {"opening", "closing"} and pos is not None:
                    # Motion started outside the bridge (HA automation etc.).
                    self._start_motion_estimate(child, entity_id)
                    continue

                snapshot = await self._async_push_state(child, entity_id)
                _LOGGER.debug(
                    "HA state event child=%s entity=%s snapshot=%s",
                    child.key,
                    entity_id,
                    snapshot,
                )

        # xiaomi_home's YS7 cover emits NO mid-travel state events, and for
        # open_cover it does not even emit an "opening" state: the entity jumps
        # straight from closed/0 to open/100 at arrival (live log 2026-11-08
        # 04:03:59 -> 04:04:06).  A motion started by an HA automation or the
        # REST API therefore never reaches the estimator via state_changed and
        # the App card stays frozen for the whole travel.  Watch cover service
        # calls as a second, independent start signal so both control paths
        # animate identically.
        cover_entities = {eid for eid in entity_ids if eid.startswith("cover.")}

        def _service_entity_ids(data: dict) -> list[str]:
            svc_data = data.get("service_data") or {}
            raw = svc_data.get("entity_id")
            if raw is None:
                target = svc_data.get("target") or {}
                raw = target.get("entity_id") if isinstance(target, dict) else None
            if isinstance(raw, str):
                return [raw]
            return [item for item in (raw or []) if isinstance(item, str)]

        async def service_called(event) -> None:
            data = event.data or {}
            if data.get("domain") != "cover" or not cover_entities:
                return
            service = data.get("service")
            for eid in _service_entity_ids(data):
                if eid not in cover_entities or eid in self._bridge_command_active:
                    # The bridge's own cloud command handler starts this
                    # motion with the App tap timestamp; do not double-start.
                    continue
                child = next(
                    (c for c in plan.children
                     if any(b.entity_id == eid for b in c.bindings)),
                    None,
                )
                if child is None:
                    continue
                if service == "stop_cover":
                    self._cancel_motion(eid)
                    held = self._last_push_pos.get(eid)
                    self._schedule_settle(child, eid, expect=held, fallback=held)
                    continue
                svc_data = data.get("service_data") or {}
                if service == "set_cover_position":
                    try:
                        target_pos = max(
                            0, min(100, int(svc_data.get("position", 0)))
                        )
                    except (TypeError, ValueError):
                        continue
                elif service == "open_cover":
                    target_pos = 100
                elif service == "close_cover":
                    target_pos = 0
                else:
                    continue
                _COVER_TARGETS[eid] = target_pos
                _LOGGER.debug(
                    "External cover service child=%s entity=%s service=%s "
                    "target=%s",
                    child.key, eid, service, target_pos,
                )
                self._start_motion_estimate(child, eid, t0=time.monotonic())

        unsub_state = async_track_state_change_event(
            self._hass, entity_ids, state_changed
        )
        unsub_service = self._hass.bus.async_listen("call_service", service_called)

        def unsubscribe() -> None:
            unsub_state()
            unsub_service()

        return unsubscribe

    # ------------------------------------------------------------------
    # Mid-motion position interpolation
    #
    # HA cover entities (xiaomi_home) emit no state_changed events while a
    # curtain travels; current_position only jumps at completion. Huawei H5
    # cards render the progress bar from streamed opener.current values, so we
    # synthesize intermediate snapshots while moving and push one settled
    # read-back after the movement ends.
    # ------------------------------------------------------------------

    def _cancel_motion(self, entity_id: str) -> None:
        task = self._motion_tasks.pop(entity_id, None)
        if task is not None:
            task.cancel()
        self._motion_start.pop(entity_id, None)
        self._motion_action.pop(entity_id, None)
        self._motion_ctx.pop(entity_id, None)
        handle = self._settle_handles.pop(entity_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_settle(self, child: ChildPlan, entity_id: str,
                         delay: float = 0.8, *,
                         expect: int | None = None,
                         fallback: int | None = None) -> None:
        if self._state_callback is None:
            return
        self._settle_handles.pop(entity_id, None)

        async def _push(_now=None):
            self._settle_handles.pop(entity_id, None)
            snapshot = await self._async_push_settled(
                child, entity_id, fallback=fallback, expect=expect
            )
            _LOGGER.debug(
                "Settled push child=%s entity=%s snapshot=%s",
                child.key, entity_id, snapshot,
            )

        # async_call_later accepts coroutine functions and runs them on the
        # event loop; do NOT use hass.async_create_task from timer callbacks.
        self._settle_handles[entity_id] = async_call_later(
            self._hass, delay, _push
        )

    # ------------------------------------------------------------------
    # Active MIoT property polling (real position instead of interpolation)
    # ------------------------------------------------------------------

    def _resolve_xiaomi_probe(self, entity_id: str) -> dict[str, Any] | None:
        """Find the MIoTClient / LAN channel behind a xiaomi_home cover."""
        if entity_id in _XIAOMI_PROBE_CACHE:
            return _XIAOMI_PROBE_CACHE[entity_id]
        probe = None
        bucket = self._hass.data.get("xiaomi_home") or {}
        clients = list((bucket.get("miot_clients") or {}).values())
        # xiaomi_home entity ids embed the numeric did, e.g.
        # cover.dooya_cn_873865751_ys7_s_2_curtain
        match = re.search(r"_(\d{6,12})_", entity_id)
        did = match.group(1) if match else None
        if clients and did:
            # Several curtains may belong to DIFFERENT cloud accounts, i.e.
            # different MIoTClient instances.  Pick the client that actually
            # owns this did instead of assuming clients[0] (single-device
            # assumption from the first implementation).
            for client in clients:
                cache = getattr(client, "_device_list_cache", None) or {}
                if did not in cache:
                    continue
                probe = {
                    "client": client,
                    "did": did,
                    "siid": _XIAOMI_POSITION_PROP[0],
                    "piid": _XIAOMI_POSITION_PROP[1],
                    # MIoTClient.miot_lan -> MIoTLan.get_prop_async() sends
                    # get_properties straight to the motor over UDP 54321.
                    "lan": getattr(client, "miot_lan", None),
                }
                break
        _XIAOMI_PROBE_CACHE[entity_id] = probe
        _LOGGER.debug(
            "Xiaomi LAN probe entity=%s did=%s clients=%s resolved=%s lan=%s",
            entity_id, did, len(clients), probe is not None,
            probe is not None and probe.get("lan") is not None,
        )
        return probe

    @staticmethod
    def _coerce_position(raw: Any) -> int | None:
        if isinstance(raw, dict):
            raw = raw.get("value")
        elif isinstance(raw, list) and raw:
            first = raw[0]
            raw = first.get("value") if isinstance(first, dict) else first
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return max(0, min(100, int(round(raw))))

    async def _async_read_real_cover_position(
        self, entity_id: str
    ) -> int | None:
        """Read the motor's LIVE position over the local LAN (0-100)."""
        probe = self._resolve_xiaomi_probe(entity_id)
        lan = probe.get("lan") if probe else None
        if lan is None:
            return None
        try:
            raw = await asyncio.wait_for(
                lan.get_prop_async(
                    did=probe["did"],
                    siid=probe["siid"],
                    piid=probe["piid"],
                    timeout_ms=_XIAOMI_LAN_TIMEOUT_MS,
                ),
                timeout=_XIAOMI_LAN_TIMEOUT_MS / 1000.0 + 0.35,
            )
        except Exception as err:  # LAN offline / timeout / not ready
            _LOGGER.debug(
                "LAN cover position read failed entity=%s err=%s",
                entity_id, err,
            )
            return None
        value = self._coerce_position(raw)
        _LOGGER.debug(
            "LAN cover position entity=%s raw=%s value=%s",
            entity_id, raw, value,
        )
        return value

    async def _async_read_cloud_shadow(self, entity_id: str) -> int | None:
        """Cloud shadow value, for the startup comparison log only."""
        probe = self._resolve_xiaomi_probe(entity_id)
        if probe is None:
            return None
        try:
            raw = await asyncio.wait_for(
                probe["client"].get_prop_async(
                    did=probe["did"],
                    siid=probe["siid"],
                    piid=probe["piid"],
                ),
                timeout=2.0,
            )
        except Exception as err:
            _LOGGER.debug("Cloud shadow read failed entity=%s err=%s",
                          entity_id, err)
            return None
        return self._coerce_position(raw)

    def _start_motion_estimate(self, child: ChildPlan, entity_id: str,
                               *, t0: float | None = None) -> None:
        if self._state_callback is None:
            return
        state = self._hass.states.get(entity_id)
        if state is None:
            return
        start_pos = state.attributes.get("current_position")
        if not isinstance(start_pos, (int, float)):
            return
        target_pos = _COVER_TARGETS.get(entity_id)
        if target_pos is None:
            target_pos = 100 if state.state == "opening" else 0
        start_pos, target_pos = int(start_pos), int(target_pos)
        distance = abs(target_pos - start_pos)
        if distance <= 0:
            return
        self._cancel_motion(entity_id)
        # Motions started outside the bridge (HA automation / REST API) never
        # pass through _call_binding, so _COVER_TARGETS would stay empty and
        # state_changed() could not recognise the motor's arrival event.
        # Publish the inferred endpoint here for BOTH paths.
        _COVER_TARGETS[entity_id] = target_pos
        full = self._travel_seconds.get(entity_id, _DEFAULT_FULL_TRAVEL_SECONDS)
        est_seconds = max(full * distance / 100.0, _MIN_MOTION_SECONDS)
        ctx = {
            # "Button pressed": use the cloud command arrival timestamp when
            # available; otherwise the HA state event that started the motion.
            "t0": t0 if t0 is not None else time.monotonic(),
            "start": start_pos,
            "target": target_pos,
            "distance": distance,
            "est": est_seconds,
            "full": full,
        }
        self._motion_ctx[entity_id] = ctx
        self._motion_start[entity_id] = start_pos
        # Huawei motor mode while travelling: 1 = opening, 0 = closing.
        self._motion_action[entity_id] = 1 if target_pos > start_pos else 0
        self._motion_tasks[entity_id] = asyncio.create_task(
            self._motion_estimator(child, entity_id, start_pos, target_pos, ctx)
        )
        _LOGGER.debug(
            "Motion start child=%s entity=%s %s->%s full=%.2fs "
            "planned=%.2fs ticks=%s",
            child.key, entity_id, start_pos, target_pos, full,
            est_seconds, max(int(round(est_seconds / _TICK_INTERVAL)), 3),
        )

    async def _motion_estimator(self, child: ChildPlan, entity_id: str,
                                start_pos: int, target_pos: int,
                                ctx: dict[str, Any]) -> None:
        distance = ctx["distance"]
        if distance <= 0:
            return
        ticks = max(int(round(ctx["est"] / _TICK_INTERVAL)), 3)
        step = (target_pos - start_pos) / ticks
        arrived = False
        # True fixed cadence.  LAN polling runs in a BACKGROUND task and its
        # result is consumed at the next frame, so a slow/timeout miIO read
        # can never stretch an animation frame (the old inline poll produced
        # ~0.9-1.0 s frames from a nominal 0.75 s interval).
        cadence = ctx["t0"] + _TICK_INTERVAL
        deadline = ctx["t0"] + ctx["est"] + _ARRIVAL_GRACE_SECONDS
        lan_pending: asyncio.Task | None = None
        lan_seen: tuple[int, float] | None = None

        async def _read_lan_with_ts():
            value = await self._async_read_real_cover_position(entity_id)
            return value, time.monotonic()

        def _poll_lan() -> None:
            nonlocal lan_pending
            if lan_pending is None:
                lan_pending = asyncio.create_task(_read_lan_with_ts())

        def _consume_lan() -> None:
            nonlocal lan_pending, lan_seen
            lan_seen = None  # each LAN result is applied to exactly one frame
            if lan_pending is None or not lan_pending.done():
                return
            try:
                value, ts = lan_pending.result()
            except Exception:
                value, ts = None, None
            lan_pending = None
            if value is not None and ts is not None:
                lan_seen = (value, ts)

        async def _sleep_to_cadence() -> None:
            nonlocal cadence
            await asyncio.sleep(max(0.0, cadence - time.monotonic()))
            cadence += _TICK_INTERVAL
            if cadence < time.monotonic():
                # Never emit a burst of catch-up frames after a slow tick.
                cadence = time.monotonic() + _TICK_INTERVAL

        async def _push_tick(index: int | None) -> bool:
            """Emit one animation frame; True when the motor just arrived."""
            _consume_lan()
            _poll_lan()
            est_pos = (
                max(0, min(100, round(start_pos + step * index)))
                if index is not None else None
            )
            real = None
            arrived_at = None
            if lan_seen is not None:
                value, ts = lan_seen
                # Measured 2026-11-08: the YS7 motor only publishes
                # current-position on ARRIVAL; during travel even a direct LAN
                # read returns the frozen pre-movement value.  Never let that
                # push the progress bar backwards.
                if (
                    value != start_pos
                    and min(start_pos, target_pos) <= value
                    <= max(start_pos, target_pos)
                ):
                    real, arrived_at = value, ts
            source = "estimate"
            pos = est_pos
            arrived_now = False
            if real is not None and abs(real - target_pos) <= 2:
                # FIRST truth of the final state = calibration timestamp.
                arrived_now = True
                pos = target_pos
                source = "LAN arrival"
                self._learn_travel_time(entity_id, "LAN", arrived_at)
            elif real is not None:
                pos = real
                source = "LAN"
            elif pos is None:
                # Grace phase: hold just short of the endpoint while waiting
                # for the motor to confirm, keeping the card in running mode.
                hold = target_pos + (2 if target_pos < start_pos else -2)
                pos = max(0, min(100, hold))
                source = "hold"
            _LOGGER.debug(
                "Motion %s child=%s pos=%s/%s t=%.2fs planned=%.2fs action=%s",
                source, child.key, pos, target_pos,
                time.monotonic() - ctx["t0"], ctx["est"],
                self._motion_action.get(entity_id),
            )
            await self._async_push_state(child, entity_id, position=pos)
            return arrived_now

        try:
            for index in range(1, ticks):
                await _sleep_to_cadence()
                if await _push_tick(index):
                    arrived = True
                    break
            # The learned duration can still be slightly short.  Stay in
            # "running" mode for a bounded grace window rather than declaring
            # "已关闭/已打开" before the motor actually finishes.  The HA
            # arrival event (state_changed) may also cancel this task.
            while not arrived and time.monotonic() < deadline:
                await _sleep_to_cadence()
                if await _push_tick(None):
                    arrived = True
                    break
        finally:
            if lan_pending is not None:
                lan_pending.cancel()
            # Only the CURRENT estimator may clear the running marker.  A
            # superseded task whose cancellation lands late must not wipe the
            # newer motion's action/target state.
            if self._motion_tasks.get(entity_id) is asyncio.current_task():
                self._motion_tasks.pop(entity_id, None)
                self._motion_start.pop(entity_id, None)
                self._motion_action.pop(entity_id, None)
                if self._motion_ctx.get(entity_id) is ctx:
                    self._motion_ctx.pop(entity_id, None)
        snapshot = await self._async_push_settled(
            child, entity_id, fallback=target_pos, expect=target_pos
        )
        _LOGGER.debug(
            "Motion settled read child=%s entity=%s arrived=%s snapshot=%s",
            child.key, entity_id, arrived, snapshot,
        )

    def schedule_startup_sync(self, plan, callback, *, attempts: int = 12):
        """Retry initial snapshots while HA entities/cloud session restore."""

        remaining = attempts
        cancel_handle = None

        async def retry(_now) -> None:
            nonlocal remaining, cancel_handle
            pending = False
            for child in plan.children:
                snapshot = await self._async_read_child_state(child)
                if snapshot:
                    _LOGGER.debug(
                        "Startup state snapshot child=%s state=%s",
                        child.key,
                        snapshot,
                    )
                    if callback(child.key, snapshot) is False:
                        pending = True
                else:
                    pending = True
            remaining -= 1
            if pending and remaining > 0:
                cancel_handle = async_call_later(self._hass, 5, retry)

        cancel_handle = async_call_later(self._hass, 2, retry)

        def cancel() -> None:
            if cancel_handle is not None:
                cancel_handle()

        return cancel

    def _run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._hass.loop)
        return future.result(timeout=15)

    async def _async_read_child_state(
        self, child: ChildPlan
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for binding in child.bindings:
            if binding.source in {"constant", "command"}:
                value = _read_binding_value("", {}, binding)
                state_value = f"<{binding.source}>"
            else:
                state = self._hass.states.get(binding.entity_id)
                if state is None or state.state in {"unknown", "unavailable"}:
                    continue
                state_value = state.state
                value = _read_binding_value(state.state, state.attributes, binding)
            _LOGGER.debug(
                "HA binding read child=%s sid=%s field=%s entity=%s "
                "state=%s attribute=%s converted=%s",
                child.key,
                binding.service_id,
                binding.field_name,
                binding.entity_id,
                state_value,
                binding.attribute,
                value,
            )
            if value is None:
                continue
            result.setdefault(binding.service_id, {})[binding.field_name] = value
        return result

    async def _async_apply_child_command(
        self, child: ChildPlan, service_id: str, payload: dict[str, object]
    ) -> None:
        # Timestamp as close to the physical button tap as the bridge can get
        # (cloud command arrival).  It is the t0 for travel-time learning.
        command_t0 = time.monotonic()
        declared = {b.field_name for b in child.bindings}

        # 2026-11-08: entering the 2DCY H5 card makes the App push its timer
        # configuration as sid=timer, e.g.
        #   {"action": 1, "timer": [{"para": "target", "paraValue": 100, ...}]}
        # Here `action` is the TIMER's own flag, not the curtain service.  The
        # old unconditional field-name fallback mapped it to cover.open_cover,
        # so merely opening the card opened a closed curtain.  Any payload
        # carrying structured (list/dict) fields this child does not declare
        # is a config/report message, never a control command.
        config_fields = [
            key for key, value in payload.items()
            if key not in declared and isinstance(value, (list, dict))
        ]
        if config_fields:
            _LOGGER.warning(
                "Ignored H5 config message child=%s sid=%s "
                "config_fields=%s payload_keys=%s",
                child.key,
                service_id,
                config_fields,
                list(payload),
            )
            return

        matching = [b for b in child.bindings if b.service_id == service_id]
        if not any(b.field_name in payload for b in matching):
            # Huawei rich (H5) cards sometimes send a command under a different
            # sid than the one declared in mapping. Fall back to matching purely
            # by characteristic name so `on` is never silently ignored -- but
            # only when the payload has no unrelated fields and the field is
            # not a motion command (those require an exact sid match).
            extra = [key for key in payload if key not in declared]
            fallback = [] if extra else [
                b for b in child.bindings
                if b.field_name in payload
                and b.entity_id
                and b.field_name not in _STRICT_SID_FIELDS
            ]
            if fallback:
                _LOGGER.warning(
                    "Command sid=%s matched no field; field-name fallback "
                    "child=%s fields=%s payload=%s",
                    service_id,
                    child.key,
                    [b.field_name for b in fallback],
                    payload,
                )
                matching = fallback
            else:
                _LOGGER.warning(
                    "Ignored cloud command child=%s sid=%s payload=%s "
                    "extra=%s: no safe control field matched",
                    child.key,
                    service_id,
                    payload,
                    extra,
                )
                return
        for binding in matching:
            if binding.field_name not in payload:
                continue
            value = _map_huawei_value(binding, payload[binding.field_name])
            cover_entity = (
                binding.entity_id
                if binding.entity_id
                and binding.entity_id.startswith("cover.")
                else None
            )
            if cover_entity:
                self._bridge_command_active.add(cover_entity)
            try:
                await _call_binding(self._hass, binding, value)
            finally:
                if cover_entity:
                    self._bridge_command_active.discard(cover_entity)

        # A1EI 音乐主机（0BD）：上一首/下一首是瞬时动作。App 下发后云端把
        # desired 置 1（切换中）并等待设备 REPORT 对账；真机时序是先报
        # 1（切换中）持续约 1~2s 再报 0（切换完成）。这两个字段 source=command
        # 不进常规快照，避免常规 0 与对账 1 在 1ms 内互相覆盖，由这里单独推
        # 两段式：立即报 1，2 秒后报 0。
        if service_id in _MOMENTARY_ACTION_SIDS and self._state_callback is not None:
            running = await self._async_read_child_state(child)
            running.setdefault(service_id, {})["action"] = 1
            self._state_callback(child.key, running)

            async def _push_finished(_now=None):
                snapshot = await self._async_read_child_state(child)
                snapshot.setdefault(service_id, {})["action"] = 0
                self._state_callback(child.key, snapshot)
                _LOGGER.debug(
                    "Action finished push child=%s sid=%s snapshot=%s",
                    child.key,
                    service_id,
                    snapshot,
                )

            async_call_later(self._hass, 1.5, _push_finished)

        # Cover motion: HA cover entities (xiaomi_home) emit NO mid-travel
        # updates — for close_cover not even a "closing" state event. Start the
        # position estimator right here, where we know motion begins.
        for binding in matching:
            field = binding.field_name
            if field not in payload or not binding.entity_id:
                continue
            if not binding.entity_id.startswith("cover."):
                continue
            try:
                action_val = int(payload[field])
            except (TypeError, ValueError):
                action_val = None
            if field == "action" and action_val == 2:
                # stop/pause: no arrival timestamp exists, so this motion is
                # deliberately NOT used for calibration.  Keep the last
                # animated position, because a LAN read right after a stop
                # still returns the frozen pre-movement value.
                self._cancel_motion(binding.entity_id)
                held = self._last_push_pos.get(binding.entity_id)
                self._schedule_settle(
                    child, binding.entity_id,
                    expect=held, fallback=held,
                )
            else:
                self._start_motion_estimate(
                    child, binding.entity_id, t0=command_t0
                )


def _read_binding_value(
    state_value: str, attributes: dict[str, Any], binding: BindingField
) -> object | None:
    if binding.source == "constant":
        value: object = binding.value
    elif binding.source == "command":
        return None
    elif binding.source == "state":
        value: object = state_value
    elif binding.source == "attribute" and binding.attribute:
        value = attributes.get(binding.attribute)
    else:
        return None
    if binding.component is not None:
        if not isinstance(value, (tuple, list)) or len(value) < 3:
            return None
        component_index = {"red": 0, "green": 1, "blue": 2}[binding.component]
        value = value[component_index]
    mapping = binding.ha_to_huawei.get("map")
    if isinstance(mapping, dict):
        value = mapping.get(str(value), value)
    if (binding.service_id, binding.field_name) in _FLOAT_CHARACTERISTICS and isinstance(value, str):
        try:
            value = round(float(value), 1)
        except ValueError:
            pass
    elif binding.field_name in _INT_CHARACTERISTICS and isinstance(value, str):
        try:
            value = int(round(float(value)))
        except ValueError:
            pass
    if binding.field_name == "brightness" and isinstance(value, (int, float)):
        return max(0, min(100, round(float(value) * 100 / 255)))
    # media_player volume_level (0.0~1.0) -> 华为 volume (0~100)
    if binding.field_name == "volume" and isinstance(value, (int, float)):
        return max(0, min(100, int(round(float(value) * 100))))
    return value


def _map_huawei_value(binding: BindingField, value: object) -> object:
    mapping = binding.huawei_to_ha.get("map")
    if isinstance(mapping, dict):
        return mapping.get(str(value), value)
    return value


async def _call_binding(
    hass: HomeAssistant, binding: BindingField, value: object
) -> None:
    if not binding.entity_id:
        return
    domain, _separator, _object_id = binding.entity_id.partition(".")
    if not domain or not _object_id:
        return
    if binding.field_name == "on":
        is_on = value not in (None, False, 0, "0", "off", "false")
        if domain == "lock":
            service = "lock" if is_on else "unlock"
            _LOGGER.debug(
                "Calling lock.%s entity=%s value=%s",
                service,
                binding.entity_id,
                value,
            )
            await hass.services.async_call(
                "lock", service, {"entity_id": binding.entity_id}, blocking=True
            )
            return
        if domain in {"button", "input_button"}:
            if is_on:
                _LOGGER.debug(
                    "Calling %s.press entity=%s", domain, binding.entity_id
                )
                await hass.services.async_call(
                    domain, "press", {"entity_id": binding.entity_id}, blocking=True
                )
            return
        service = "turn_on" if is_on else "turn_off"
        _LOGGER.debug(
            "Calling HA service %s.%s entity=%s value=%s",
            domain,
            service,
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            domain, service, {"entity_id": binding.entity_id}, blocking=True
        )
        return
    if binding.field_name == "brightness":
        brightness = max(0, min(100, int(value)))
        _LOGGER.debug(
            "Calling HA service %s.turn_on entity=%s brightness_pct=%s",
            domain,
            binding.entity_id,
            brightness,
        )
        await hass.services.async_call(
            domain,
            "turn_on",
            {"entity_id": binding.entity_id, "brightness_pct": brightness},
            blocking=True,
        )
        return
    if binding.field_name == "colorTemperature":
        _LOGGER.debug(
            "Calling HA service %s.turn_on entity=%s color_temp_kelvin=%s",
            domain,
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            domain,
            "turn_on",
            {"entity_id": binding.entity_id, "color_temp_kelvin": int(value)},
            blocking=True,
        )
        return
    if domain == "number":
        _LOGGER.debug(
            "Calling number.set_value entity=%s value=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": binding.entity_id, "value": float(value)},
            blocking=True,
        )
        return
    if domain == "select":
        _LOGGER.debug(
            "Calling select.select_option entity=%s option=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": binding.entity_id, "option": str(value)},
            blocking=True,
        )
        return
    if domain == "media_player" and binding.field_name == "volume":
        level = max(0.0, min(1.0, float(value) / 100.0))
        _LOGGER.debug(
            "Calling media_player.volume_set entity=%s volume_level=%s",
            binding.entity_id,
            level,
        )
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": binding.entity_id, "volume_level": level},
            blocking=True,
        )
        return
    if domain == "media_player" and binding.field_name == "play":
        playing = value not in (None, False, 0, "0", "off", "false")
        service = "media_play" if playing else "media_pause"
        _LOGGER.debug(
            "Calling media_player.%s entity=%s value=%s",
            service,
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "media_player",
            service,
            {"entity_id": binding.entity_id},
            blocking=True,
        )
        return
    if domain in {"button", "input_button"} and binding.field_name == "action":
        _LOGGER.debug(
            "Calling %s.press entity=%s (action button)",
            domain,
            binding.entity_id,
        )
        await hass.services.async_call(
            domain, "press", {"entity_id": binding.entity_id}, blocking=True
        )
        return
    if domain == "media_player" and binding.field_name == "action":
        # commonExecution1=上一首 / commonExecution2=下一首（A1EI 音乐主机）。
        # 注意：HOPE 集成的 previous/next button 只控制镜像(次)播放器，
        # 主播放器切歌必须走 media_player 的 track 服务。
        # 仅值 1（切换中/启动）触发；App 超时后可能补发 3（暂停），不得重复切歌。
        try:
            trigger = int(value) == 1
        except (TypeError, ValueError):
            trigger = False
        if not trigger:
            _LOGGER.debug(
                "Ignoring action value=%s child sid=%s (not a start command)",
                value,
                binding.service_id,
            )
            return
        service = (
            "media_previous_track"
            if binding.service_id == "commonExecution1"
            else "media_next_track"
        )
        _LOGGER.debug(
            "Calling media_player.%s entity=%s (action sid=%s)",
            service,
            binding.entity_id,
            binding.service_id,
        )
        await hass.services.async_call(
            "media_player",
            service,
            {"entity_id": binding.entity_id},
            blocking=True,
        )
        return
    if domain == "climate" and binding.field_name == "mode":
        _LOGGER.debug(
            "Calling climate.set_hvac_mode entity=%s hvac_mode=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": binding.entity_id, "hvac_mode": str(value)},
            blocking=True,
        )
        return
    if domain == "climate" and binding.field_name == "target":
        _LOGGER.debug(
            "Calling climate.set_temperature entity=%s temperature=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": binding.entity_id, "temperature": float(value)},
            blocking=True,
        )
        return
    if domain == "climate" and binding.field_name == "gear":
        _LOGGER.debug(
            "Calling climate.set_fan_mode entity=%s fan_mode=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": binding.entity_id, "fan_mode": str(value)},
            blocking=True,
        )
        return
    if domain == "climate" and binding.field_name == "direction":
        _LOGGER.debug(
            "Calling climate.set_swing_mode entity=%s swing_mode=%s",
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "climate",
            "set_swing_mode",
            {"entity_id": binding.entity_id, "swing_mode": str(value)},
            blocking=True,
        )
        return
    if domain == "cover" and binding.field_name == "action":
        services = {0: "close_cover", 1: "open_cover", 2: "stop_cover"}
        service = services.get(int(value))
        if service is None:
            return
        if service == "close_cover":
            _COVER_TARGETS[binding.entity_id] = 0
        elif service == "open_cover":
            _COVER_TARGETS[binding.entity_id] = 100
        _LOGGER.debug(
            "Calling cover.%s entity=%s action=%s",
            service,
            binding.entity_id,
            value,
        )
        await hass.services.async_call(
            "cover",
            service,
            {"entity_id": binding.entity_id},
            blocking=True,
        )
        return
    if domain == "cover" and binding.field_name == "target":
        _LOGGER.debug(
            "Calling cover.set_cover_position entity=%s position=%s",
            binding.entity_id,
            value,
        )
        _COVER_TARGETS[binding.entity_id] = max(0, min(100, int(value)))
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": binding.entity_id, "position": int(value)},
            blocking=True,
        )
        return
    if binding.field_name in {"red", "green", "blue"}:
        state = hass.states.get(binding.entity_id)
        current = state.attributes.get("rgb_color") if state is not None else None
        rgb = list(current) if isinstance(current, (tuple, list)) and len(current) >= 3 else [0, 0, 0]
        component_index = {"red": 0, "green": 1, "blue": 2}[binding.field_name]
        rgb[component_index] = max(0, min(255, int(value)))
        _LOGGER.debug(
            "Calling HA service %s.turn_on entity=%s rgb_color=%s",
            domain,
            binding.entity_id,
            rgb,
        )
        await hass.services.async_call(
            domain,
            "turn_on",
            {"entity_id": binding.entity_id, "rgb_color": tuple(rgb)},
            blocking=True,
        )

