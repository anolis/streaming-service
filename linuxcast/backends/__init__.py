from linuxcast.backends.airplay import AirPlayBackend
from linuxcast.backends.airplay_native import AirPlayNativeBackend
from linuxcast.backends.chromecast import ChromecastBackend
from linuxcast.backends.dlna import DlnaBackend
from linuxcast.backends.miracast import MiracastBackend

BACKENDS = {b.name: b for b in (ChromecastBackend(), DlnaBackend(), MiracastBackend(), AirPlayBackend(), AirPlayNativeBackend())}
