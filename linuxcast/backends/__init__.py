from linuxcast.backends.airplay import AirPlayBackend
from linuxcast.backends.chromecast import ChromecastBackend
from linuxcast.backends.dlna import DlnaBackend
from linuxcast.backends.miracast import MiracastBackend

BACKENDS = {b.name: b for b in (ChromecastBackend(), DlnaBackend(), MiracastBackend(), AirPlayBackend())}
