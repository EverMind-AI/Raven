"""The gateway control plane's channel methods.

Three methods, one consumer: :mod:`raven.gateway.live_probe` asks a running
gateway about its live channel adapters -- the one question no other process
can answer, because the adapters exist only here. The forty-odd config-admin
methods that used to share this module served the retired ``ui-webui`` front
end and were deleted with it (C7 ruling, 2026-08-29); a browser manages
channels through the terminal dialect's console methods, which reach these
three through the probe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _render_qr_png(text: str) -> str | None:
    """A scan payload as a PNG data URI, so a client shows the login QR with no
    QR library of its own.

    ``qrcode`` only ships with the QR-login channel extras, so an install that
    enabled such a channel some other way still has to degrade rather than 500:
    None tells the caller to fall back to handing the raw payload to the client.
    """
    import base64
    import io

    try:
        import qrcode
    except ImportError:
        return None

    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def register_config_methods(
    dispatcher: "Dispatcher",
    *,
    channel_manager: Any,
) -> None:
    """Register the live-channel methods on the web dispatcher."""
    from raven.config.update_channels import channel_names

    async def _channels_qr(params: dict) -> dict:
        """Login QR for a QR-login channel (weixin/whatsapp) as a PNG data URI,
        plus whether the account is actually paired. Empty when there is no live
        manager, no such channel, or nothing pending (e.g. already paired).

        ``connected`` reads the adapter's own login state, never "the task is
        running and no QR is pending yet" -- both adapters flip ``_running``
        before the first QR is fetched, so inferring it would report an unpaired
        channel as connected during that window and stop the UI from polling.
        ``qr_text`` carries the raw payload for the client to render when the
        gateway has no ``qrcode`` to rasterise with.
        """
        name = params.get("name", "")
        ch = channel_manager.get_channel(name) if channel_manager is not None else None
        qr = getattr(ch, "pending_qr", None) if ch is not None else None
        running = ch is not None and bool(getattr(ch, "is_running", False))
        png = _render_qr_png(qr) if qr else None
        # The rebind snapshot rides this poll rather than needing its own: the
        # client is already asking once a second for the code, and phase + code
        # age are what turn "a picture" into "scan it, it expires, we reissued".
        rebind = ch.rebind_state() if ch is not None and hasattr(ch, "rebind_state") else None
        return {
            "qr": png,
            "qr_text": qr if qr and png is None else None,
            "connected": running and bool(getattr(ch, "connected", False)),
            "running": running,
            "rebind": rebind,
        }

    dispatcher.register("raven.channels.qr", _channels_qr)

    async def _channels_start(params: dict) -> dict:
        """Start (or stop) one channel's adapter now, without a gateway restart.

        The switch is written by whichever process the reader is talking to --
        the page's ``raven serve``, the TUI, the CLI -- and the adapter only
        exists here. So enabling a channel used to mean nothing until the next
        launch: no adapter, no QR to scan, no messages. ``outcome`` is a word
        from ChannelManager, every value of which is a state the caller draws.
        """
        name = str(params.get("name") or "")
        want_on = params.get("enabled")
        if channel_manager is None:
            return {"outcome": "no_manager"}
        if want_on is False:
            return {"outcome": await channel_manager.stop_one(name)}
        return {"outcome": await channel_manager.start_one(name)}

    dispatcher.register("raven.channels.start", _channels_start)

    async def _channels_live(params: dict) -> dict:
        """Every channel's runtime state in one round trip, for a client drawing
        a list of them.

        ``connected`` is deliberately three-valued. Only the QR adapters
        (weixin, whatsapp) know whether an account is paired; the rest expose
        ``is_running`` and nothing more. Reporting ``false`` for those would say
        "not connected" about a Telegram bot that is happily serving messages --
        the same lie as reading the config flag, wearing a different hat. Null
        means the channel does not report a pairing, and the caller must not
        render it as a negative.
        """
        out: dict[str, Any] = {}
        for name in channel_names():
            ch = channel_manager.get_channel(name) if channel_manager is not None else None
            if ch is None:
                out[name] = {"running": False, "connected": None, "qr_login": False}
                continue
            reports_pairing = hasattr(type(ch), "connected")
            out[name] = {
                "running": bool(getattr(ch, "is_running", False)),
                "connected": bool(getattr(ch, "connected", False)) if reports_pairing else None,
                "qr_login": hasattr(ch, "pending_qr"),
            }
        return {"channels": out}

    dispatcher.register("raven.channels.live", _channels_live)


__all__ = ["register_config_methods"]
