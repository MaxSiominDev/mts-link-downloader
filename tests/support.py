MASTER_URL = "https://delivery.example/record/2026/09/10/abc.mp4/playlist.m3u8"
AUDIO_URL = "https://delivery.example/record/2026/09/10/abc.mp4/a1/index.m3u8"

CAMERA_MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",AUTOSELECT=YES,NAME="eng",URI="a1/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=713861,RESOLUTION=640x480,CODECS="mp4a.40.2,vp09.00.30.08",AUDIO="audio"
v1/index.m3u8
"""

SCREEN_MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=1160452,RESOLUTION=1600x900,CODECS="avc1.42C028"
v1/index.m3u8
"""

MUXED_MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x480,CODECS="mp4a.40.2,avc1.42C028"
v1/index.m3u8
"""

AUDIO = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA-SEQUENCE:1
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-TARGETDURATION:16
#EXT-X-MAP:URI="init/1.m4s"
#EXTINF:11.626,
media/1.m4s
#EXTINF:3.582,
media/2.m4s
#EXT-X-ENDLIST
"""


def read(path: str) -> bytes:
    with open(path, "rb") as source:
        return source.read()


def write(path: str, data: bytes) -> None:
    with open(path, "wb") as out:
        out.write(data)
