from linuxcast.backends.airplay import AirPlayBackend
from linuxcast.backends.chromecast import ChromecastBackend
from linuxcast.backends.miracast import MiracastBackend

BACKENDS = {b.name: b for b in (ChromecastBackend(), MiracastBackend(), AirPlayBackend())}
