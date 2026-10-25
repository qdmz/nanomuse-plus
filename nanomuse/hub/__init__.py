"""This device on the nanoMuse hub: the socket to the relay (`client`), what it does for the
other devices (`actions`), and the service that bridges their tasks into side chats
(`service`). Protocol: docs/hub.md; the shape of every device: docs/every-device.md."""

from nanomuse.hub.client import HubClient, HubError, IncomingCall

__all__ = ["HubClient", "HubError", "IncomingCall"]
