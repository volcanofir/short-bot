"""Fugle tick recorder for Short Bot research.

Primary path: Fugle MarketData WebSocket trades channel.
Gap recovery: Fugle intraday trades REST endpoint after subscribe/reconnect.

This module never places orders. "Smart touch" means the market printed at or
through the expected sell-limit price; it is not a guarantee that a real queued
order would have filled.
"""

import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
import websocket


class FugleTickRecorder:
    WS_URL = "wss://api.fugle.tw/marketdata/v1.0/stock/streaming"
    REST_TRADES_URL = "https://api.fugle.tw/marketdata/v1.0/stock/intraday/trades/{code}"

    def __init__(
        self,
        api_key,
        supabase_url,
        supabase_publishable_key,
        ingest_token,
        tz,
        logger,
    ):
        self.api_key = api_key or ""
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_publishable_key = supabase_publishable_key
        self.ingest_token = ingest_token or ""
        self.tz = tz
        self.logger = logger

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._audits = {}
        self._symbol_ids = defaultdict(set)
        self._seen_serials = defaultdict(set)
        self._tick_buffer = {}
        self._dirty_audits = set()

        self._ws = None
        self._ws_connected = False
        self._authenticated = False
        self._subscriptions = {}  # code -> channel id
        self._running = False
        self._started = False
        self._last_message_at = None
        self._last_flush_at = 0.0
        self._last_error = None
        self._rest_backfill_inflight = set()

    # ------------------------------------------------------------------
    # Lifecycle / state
    # ------------------------------------------------------------------
    def start(self):
        if self._started or not self.api_key or not self.ingest_token:
            if not self.api_key:
                self.logger.info("Tick recorder disabled: Fugle API key missing")
            if not self.ingest_token:
                self.logger.info("Tick recorder disabled: ingest token missing")
            return
        self._started = True
        self._running = True
        self._restore_active_audits()
        threading.Thread(
            target=self._connection_loop,
            daemon=True,
            name="fugle-tick-websocket",
        ).start()
        threading.Thread(
            target=self._maintenance_loop,
            daemon=True,
            name="fugle-tick-maintenance",
        ).start()
        self.logger.info("Fugle Tick Recorder TICK_V1 started")

    def status(self):
        with self._lock:
            active = [a for a in self._audits.values() if a.get("status") == "tracking"]
            return {
                "version": "TICK_V1",
                "enabled": bool(self.api_key and self.ingest_token),
                "ws_connected": self._ws_connected,
                "authenticated": self._authenticated,
                "subscriptions": sorted(self._subscriptions.keys()),
                "active_audits": len(active),
                "buffered_ticks": len(self._tick_buffer),
                "last_message_at": (
                    self._last_message_at.isoformat() if self._last_message_at else None
                ),
                "last_error": self._last_error,
            }

    def active_audits(self):
        with self._lock:
            return [dict(a) for a in self._audits.values() if a.get("status") == "tracking"]

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_audit(self, audit):
        """Register one frozen signal audit.

        audit must already contain the original V2.9 and SMART_V1 levels.
        """
        if not audit or not audit.get("id") or not audit.get("code"):
            return False
        with self._lock:
            if audit["id"] in self._audits:
                return True
            item = dict(audit)
            item.setdefault("model", "TICK_V1")
            item.setdefault("status", "tracking")
            item.setdefault("tick_count", 0)
            item.setdefault("payload", {})
            item["payload"] = dict(item.get("payload") or {})
            item["payload"].update({
                "precision": "fugle_tick",
                "source": "websocket+rest_backfill",
                "smart_touch_is_fill_guarantee": False,
            })
            self._audits[item["id"]] = item
            self._symbol_ids[item["code"]].add(item["id"])
            self._dirty_audits.add(item["id"])

        self._subscribe_code(item["code"])
        self._backfill_async(item["code"])
        return True

    # ------------------------------------------------------------------
    # Fugle websocket
    # ------------------------------------------------------------------
    def _connection_loop(self):
        while self._running:
            try:
                self._ws_connected = False
                self._authenticated = False
                self._subscriptions = {}
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.info("Tick websocket loop error: %s", exc)
            if self._running:
                time.sleep(3)

    def _on_open(self, ws):
        self._ws_connected = True
        self._send({
            "event": "auth",
            "data": {"apikey": self.api_key},
        })

    def _on_close(self, ws, code, message):
        self._ws_connected = False
        self._authenticated = False
        self._subscriptions = {}
        self.logger.info("Tick websocket closed code=%s message=%s", code, message)

    def _on_error(self, ws, error):
        self._last_error = str(error)
        self.logger.info("Tick websocket error: %s", error)

    def _on_message(self, ws, message):
        self._last_message_at = datetime.now(self.tz)
        try:
            msg = json.loads(message)
        except Exception:
            return

        event = msg.get("event")
        if event == "authenticated":
            self._authenticated = True
            self.logger.info("Tick websocket authenticated")
            for code in self._active_codes():
                self._subscribe_code(code)
            self._backfill_all_async()
            return

        if event == "subscribed":
            rows = msg.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            with self._lock:
                for row in rows:
                    if row.get("channel") != "trades":
                        continue
                    code = str(row.get("symbol") or "")
                    channel_id = row.get("id")
                    if code and channel_id:
                        self._subscriptions[code] = channel_id
                        self.logger.info("Tick subscribed %s", code)
            return

        if event == "unsubscribed":
            return

        if event == "error":
            self._last_error = str((msg.get("data") or {}).get("message") or msg)
            self.logger.info("Tick Fugle server error: %s", self._last_error)
            return

        if event == "data" and msg.get("channel") == "trades":
            data = msg.get("data") or {}
            self._process_trade(data, source="ws")

    def _send(self, payload):
        ws = self._ws
        if not ws or not self._ws_connected:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            self.logger.info("Tick websocket send error: %s", exc)
            return False

    def _subscribe_code(self, code):
        if not code or not self._authenticated:
            return
        with self._lock:
            if code in self._subscriptions:
                return
        self._send({
            "event": "subscribe",
            "data": {"channel": "trades", "symbol": code},
        })

    def _unsubscribe_code(self, code):
        with self._lock:
            channel_id = self._subscriptions.pop(code, None)
        if channel_id:
            self._send({"event": "unsubscribe", "data": {"id": channel_id}})

    # ------------------------------------------------------------------
    # REST gap recovery
    # ------------------------------------------------------------------
    def _backfill_all_async(self):
        for code in self._active_codes():
            self._backfill_async(code)

    def _backfill_async(self, code):
        with self._lock:
            if code in self._rest_backfill_inflight:
                return
            self._rest_backfill_inflight.add(code)
        threading.Thread(
            target=self._backfill_code,
            args=(code,),
            daemon=True,
            name=f"tick-backfill-{code}",
        ).start()

    def _backfill_code(self, code):
        try:
            with self._lock:
                audits = [
                    self._audits[aid]
                    for aid in self._symbol_ids.get(code, set())
                    if aid in self._audits and self._audits[aid].get("status") == "tracking"
                ]
            if not audits:
                return

            since = min(self._dt(a.get("last_tick_at") or a["signal_at"]) for a in audits)
            since_us = int(since.timestamp() * 1_000_000)
            offset = 0
            collected = []
            # Enough for short reconnect gaps while preventing runaway REST use.
            for _ in range(12):
                r = requests.get(
                    self.REST_TRADES_URL.format(code=code),
                    headers={"X-API-KEY": self.api_key},
                    params={"offset": offset, "limit": 500, "sort": "desc"},
                    timeout=10,
                )
                if r.status_code != 200:
                    self.logger.info("Tick backfill %s status=%s", code, r.status_code)
                    break
                rows = (r.json() or {}).get("data") or []
                if not rows:
                    break
                collected.extend(rows)
                valid_times = [int(x.get("time") or 0) for x in rows if x.get("time")]
                if valid_times and min(valid_times) <= since_us:
                    break
                if len(rows) < 500:
                    break
                offset += len(rows)

            collected.sort(key=lambda x: (int(x.get("time") or 0), int(x.get("serial") or 0)))
            for trade in collected:
                t = int(trade.get("time") or 0)
                if t >= since_us:
                    trade = dict(trade)
                    trade["symbol"] = code
                    self._process_trade(trade, source="rest")
        except Exception as exc:
            self.logger.info("Tick backfill error %s: %s", code, exc)
        finally:
            with self._lock:
                self._rest_backfill_inflight.discard(code)

    # ------------------------------------------------------------------
    # Tick processing
    # ------------------------------------------------------------------
    def _process_trade(self, data, source):
        if data.get("isTrial"):
            return
        code = str(data.get("symbol") or "")
        serial = data.get("serial")
        price = data.get("price")
        raw_time = data.get("time")
        if not code or serial is None or price in (None, 0) or raw_time is None:
            return

        try:
            serial = int(serial)
            price = float(price)
            trade_dt = self._from_fugle_time(raw_time)
        except Exception:
            return

        with self._lock:
            if serial in self._seen_serials[code]:
                return
            self._seen_serials[code].add(serial)
            audit_ids = list(self._symbol_ids.get(code, set()))

            active_for_tick = False
            for audit_id in audit_ids:
                audit = self._audits.get(audit_id)
                if not audit or audit.get("status") != "tracking":
                    continue
                signal_at = self._dt(audit["signal_at"])
                tracking_until = self._dt(audit["tracking_until"])
                if trade_dt < signal_at or trade_dt > tracking_until:
                    continue
                active_for_tick = True
                self._apply_tick_to_audit(audit, trade_dt, serial, price)
                self._dirty_audits.add(audit_id)

            if not active_for_tick:
                return

            key = (trade_dt.date().isoformat(), code, serial)
            self._tick_buffer[key] = {
                "trade_date": trade_dt.date().isoformat(),
                "code": code,
                "serial": serial,
                "trade_time": trade_dt.isoformat(),
                "price": price,
                "size": int(data.get("size") or 0),
                "bid": self._num_or_none(data.get("bid")),
                "ask": self._num_or_none(data.get("ask")),
                "volume": self._int_or_none(data.get("volume")),
                "flags": {
                    "source": source,
                    "isContinuous": bool(data.get("isContinuous", False)),
                    "isOpen": bool(data.get("isOpen", False)),
                    "isClose": bool(data.get("isClose", False)),
                    "isDelayedOpen": bool(data.get("isDelayedOpen", False)),
                    "isDelayedClose": bool(data.get("isDelayedClose", False)),
                },
            }

        if len(self._tick_buffer) >= 500:
            self._flush_async()

    def _apply_tick_to_audit(self, audit, trade_dt, serial, price):
        audit["tick_count"] = int(audit.get("tick_count") or 0) + 1
        audit["last_serial"] = max(int(audit.get("last_serial") or 0), serial)
        if not audit.get("first_tick_at") or trade_dt < self._dt(audit["first_tick_at"]):
            audit["first_tick_at"] = trade_dt.isoformat()
        if not audit.get("last_tick_at") or trade_dt > self._dt(audit["last_tick_at"]):
            audit["last_tick_at"] = trade_dt.isoformat()

        audit["lowest_price"] = min(
            float(audit.get("lowest_price") if audit.get("lowest_price") is not None else price),
            price,
        )
        audit["highest_price"] = max(
            float(audit.get("highest_price") if audit.get("highest_price") is not None else price),
            price,
        )

        scalp3 = self._float_or_none(audit.get("scalp3"))
        target_1r = float(audit["target_1r"])
        target_2r = float(audit["target_2r"])
        stop = float(audit["stop"])

        if scalp3 is not None and price <= scalp3:
            self._earliest_time(audit, "scalp3_hit_at", trade_dt)
        if price <= target_1r:
            self._earliest_time(audit, "baseline_1r_at", trade_dt)
        if price <= target_2r:
            self._earliest_time(audit, "baseline_2r_at", trade_dt)
            self._earliest_event(
                audit, "baseline", "2r_first", trade_dt, price
            )
        if price >= stop:
            self._earliest_time(audit, "stop_hit_at", trade_dt)
            self._earliest_event(
                audit, "baseline", "stop_first", trade_dt, price
            )

        smart_ideal = self._float_or_none(audit.get("smart_ideal"))
        smart_expires_at = self._dt_or_none(audit.get("smart_expires_at"))
        if (
            smart_ideal is not None
            and not audit.get("smart_touch_at")
            and (smart_expires_at is None or trade_dt <= smart_expires_at)
            and price >= smart_ideal
        ):
            audit["smart_touch_at"] = trade_dt.isoformat()
            audit["smart_touch_price"] = price
            audit["smart_lowest_price"] = price
            audit["smart_highest_price"] = price
            # Smart path gets a full 60 minutes after touch, capped at 11:30.
            session_end = self._session_end(self._dt(audit["signal_at"]).date())
            extended = min(trade_dt + timedelta(minutes=60), session_end)
            if extended > self._dt(audit["tracking_until"]):
                audit["tracking_until"] = extended.isoformat()

        smart_touch = self._dt_or_none(audit.get("smart_touch_at"))
        if smart_touch and trade_dt >= smart_touch:
            audit["smart_lowest_price"] = min(
                float(audit.get("smart_lowest_price") if audit.get("smart_lowest_price") is not None else price),
                price,
            )
            audit["smart_highest_price"] = max(
                float(audit.get("smart_highest_price") if audit.get("smart_highest_price") is not None else price),
                price,
            )

            smart_1r = self._float_or_none(audit.get("smart_target_1r"))
            smart_2r = self._float_or_none(audit.get("smart_target_2r"))
            if smart_1r is not None and price <= smart_1r:
                self._earliest_time(audit, "smart_1r_at", trade_dt)
            if smart_2r is not None and price <= smart_2r:
                self._earliest_time(audit, "smart_2r_at", trade_dt)
                self._earliest_event(audit, "smart", "2r_first", trade_dt, price)
            if price >= stop:
                self._earliest_time(audit, "smart_stop_at", trade_dt)
                self._earliest_event(audit, "smart", "stop_first", trade_dt, price)

    # ------------------------------------------------------------------
    # Persistence / maintenance
    # ------------------------------------------------------------------
    def _maintenance_loop(self):
        while self._running:
            try:
                now = datetime.now(self.tz)
                codes_to_unsubscribe = []
                with self._lock:
                    for audit_id, audit in self._audits.items():
                        if audit.get("status") != "tracking":
                            continue
                        if now >= self._dt(audit["tracking_until"]):
                            audit["status"] = "completed"
                            payload = dict(audit.get("payload") or {})
                            if not audit.get("baseline_first_event"):
                                payload["baseline_outcome"] = "window_end"
                            if not audit.get("smart_touch_at"):
                                payload["smart_outcome"] = "not_touched"
                            elif not audit.get("smart_first_event"):
                                payload["smart_outcome"] = "window_end"
                            audit["payload"] = payload
                            self._dirty_audits.add(audit_id)

                    for code in list(self._subscriptions):
                        if not self._code_has_active_audit(code):
                            codes_to_unsubscribe.append(code)

                for code in codes_to_unsubscribe:
                    self._unsubscribe_code(code)

                if time.time() - self._last_flush_at >= 2:
                    self._flush()

            except Exception as exc:
                self.logger.info("Tick maintenance error: %s", exc)
            time.sleep(1)

    def _flush_async(self):
        threading.Thread(
            target=self._flush,
            daemon=True,
            name="tick-supabase-flush",
        ).start()

    def _flush(self):
        with self._lock:
            if not self._tick_buffer and not self._dirty_audits:
                self._last_flush_at = time.time()
                return
            ticks = list(self._tick_buffer.values())[:2000]
            tick_keys = {
                (x["trade_date"], x["code"], x["serial"]) for x in ticks
            }
            audits = [
                dict(self._audits[aid])
                for aid in list(self._dirty_audits)[:200]
                if aid in self._audits
            ]

        try:
            r = requests.post(
                f"{self.supabase_url}/rest/v1/rpc/ingest_short_bot_ticks",
                headers={
                    "apikey": self.supabase_publishable_key,
                    "Content-Type": "application/json",
                    "x-short-bot-token": self.ingest_token,
                },
                json={"p_ticks": ticks, "p_audits": audits},
                timeout=15,
            )
            if r.status_code not in (200, 201, 204):
                self.logger.info("Tick Supabase flush status=%s body=%s", r.status_code, r.text[:200])
                return

            with self._lock:
                for key in tick_keys:
                    self._tick_buffer.pop(key, None)
                for audit in audits:
                    self._dirty_audits.discard(audit["id"])
                self._last_flush_at = time.time()
        except Exception as exc:
            self.logger.info("Tick Supabase flush error: %s", exc)

    def _restore_active_audits(self):
        try:
            r = requests.post(
                f"{self.supabase_url}/rest/v1/rpc/fetch_short_bot_active_tick_audits",
                headers={
                    "apikey": self.supabase_publishable_key,
                    "Content-Type": "application/json",
                    "x-short-bot-token": self.ingest_token,
                },
                json={},
                timeout=10,
            )
            if r.status_code != 200:
                self.logger.info("Tick restore status=%s", r.status_code)
                return
            rows = r.json() or []
            if isinstance(rows, dict):
                rows = rows.get("result") or rows.get("data") or []
            now = datetime.now(self.tz)
            with self._lock:
                for row in rows:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    if self._dt(row["tracking_until"]) < now - timedelta(minutes=5):
                        continue
                    self._audits[row["id"]] = row
                    self._symbol_ids[str(row["code"])].add(row["id"])
            self.logger.info("Tick restore: %s active audits", len(rows))
        except Exception as exc:
            self.logger.info("Tick restore error: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _active_codes(self):
        with self._lock:
            return sorted(
                code for code in self._symbol_ids
                if self._code_has_active_audit(code)
            )

    def _code_has_active_audit(self, code):
        return any(
            self._audits.get(aid, {}).get("status") == "tracking"
            for aid in self._symbol_ids.get(code, set())
        )

    def _earliest_time(self, audit, field, dt):
        old = self._dt_or_none(audit.get(field))
        if old is None or dt < old:
            audit[field] = dt.isoformat()

    def _earliest_event(self, audit, prefix, event, dt, price):
        at_field = f"{prefix}_first_event_at"
        old = self._dt_or_none(audit.get(at_field))
        if old is None or dt < old:
            audit[f"{prefix}_first_event"] = event
            audit[at_field] = dt.isoformat()
            audit[f"{prefix}_first_event_price"] = price

    def _session_end(self, day):
        return self.tz.localize(datetime(day.year, day.month, day.day, 11, 30))

    def _from_fugle_time(self, value):
        n = int(value)
        if n > 10**14:      # microseconds
            sec = n / 1_000_000
        elif n > 10**11:    # milliseconds
            sec = n / 1_000
        else:
            sec = n
        return datetime.fromtimestamp(sec, self.tz)

    def _dt(self, value):
        if isinstance(value, datetime):
            return value.astimezone(self.tz)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return self.tz.localize(dt)
        return dt.astimezone(self.tz)

    def _dt_or_none(self, value):
        if not value:
            return None
        try:
            return self._dt(value)
        except Exception:
            return None

    @staticmethod
    def _float_or_none(value):
        try:
            return None if value is None or value == "" else float(value)
        except Exception:
            return None

    @staticmethod
    def _num_or_none(value):
        try:
            return None if value is None or value == "" else float(value)
        except Exception:
            return None

    @staticmethod
    def _int_or_none(value):
        try:
            return None if value is None or value == "" else int(value)
        except Exception:
            return None
