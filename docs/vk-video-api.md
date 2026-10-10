# The VK video API

Everything here was found by watching what the vkvideo.ru web player does and by reading the answers it gets. Nothing
is official and nothing is documented by VK, so any of it can change without warning.

## The link

```
https://vkvideo.ru/video<ownerId>_<videoId>?list=<accessKey>
```

The `owner` and `id` numbers identify the video, and the `list` parameter is the access key that makes a private video
openable by link. A call recorded in VK Messenger sits in the chat as such a video: the chat address carries the same
pair in its `z` parameter as `video<ownerId>_<videoId>`, but the chat link itself is not usable for downloading. The
host is pinned to `vkvideo.ru` so that session cookies never go to a lookalike domain.

## Authentication

The video page is a single page shell that renders no data of its own. Everything comes from API calls authorized by an
OAuth-style access token, and the token is the hard part, because a session for `vkvideo.ru` does not exist in the
browser cookies at all: Chrome only holds cookies for `vk.ru`, `vk.com` and `login.vk.ru`.

```
POST https://vkvideo.ru/al_video.php?act=web_token
Content-Type: application/x-www-form-urlencoded

version=1&app_id=52461373
```

The answer is a JSON array with the identity the request was authenticated as:

```jsonc
[
  {
    "user_id": 123456789,
    "access_token": "vk1.a....",
    "expires": 1791370178,      // unix seconds, about 3 hours ahead
    "is_active": true
  }
]
```

The exchange of a vk.ru identity for a vkvideo.ru session happens inside the redirect chain this request walks: the
first answer can be a 302 into `login.vk.ru/?act=autologin...` and back, and the exchange artifacts (`httoken`,
`remixdsid` and similar) settle in the cookie jar along the way. Notably there is no classic `remixsid` for
`vkvideo.ru` even after the exchange. When the first answer instead is `{"error": true, "error_text": "access denied"}`,
repeating the same POST succeeds, because the chain has already run and the jar is settled.

Three things break it.

**One host's Cookie header is not enough.** A header copied from devtools for `vk.ru` is rejected with a redirect to
the login page carrying `validate_result=-2`. The request has to carry a domain-aware cookie jar with the cookies of
`vk.ru`, `vk.com` and `login.vk.ru`, and the jar keeps collecting across the redirects. The exact subset that would
suffice was not narrowed down.

**A browser User-Agent is required.** Without one the same request ends in an endless redirect loop instead of a
session. The UA string of a current Chrome works, no other headers matter.

**A fresh jar without any VK cookies cannot authenticate at all.** The exchange hop on `login.vk.ru` needs the logged-in
session to hand over, and an empty jar comes back with `{"error": true, "error_text": "wrong session"}`.

## Metadata

```
POST https://api.vk.ru/method/video.getByIds?v=5.289&client_id=52461373
Content-Type: application/x-www-form-urlencoded

videos=<ownerId>_<videoId>[_<accessKey>]&video_fields=files&access_token=<token>
```

The access key goes appended to the videos parameter after an underscore. The answer carries the video fields:

```jsonc
{
  "response": {
    "items": [
      {
        "title": "Team weekly",
        "date": 1791300000,          // unix seconds of creation, what the file name uses
        "duration": 3330,            // seconds, matches the decoded length
        "width": 1920, "height": 1080,
        "files": {
          "hls_ondemand": "https://vkvd249.okcdn.ru/...hls4_....m3u8",
          "dash_ondemand": "https://.../dash4_....mpd",
          "mp4_1080": "https://vkvd249.okcdn.ru/?...type=5...",
          "failover_host": "vkvd487.okcdn.ru"
        }
      }
    ]
  }
}
```

An API failure comes back as `{"error": {"error_code": ..., "error_msg": ...}}` with HTTP 200. `files` lists only what
exists: a low-resolution recording has no `mp4_1080`, and a video still being processed has no `hls_ondemand`.

## The media

`hls_ondemand` is a master playlist of six variants, from 144p to 1080p, and every variant muxes audio and video
together: there is no audio-only rendition like MTS Link has. The audio track is the same in every variant, so the
cheapest one is taken and the video is dropped on the floor. The variant URIs in the master are relative and resolve
against the master URL.

The media playlist is plain MPEG-TS: `#EXTINF` segments and `#EXT-X-ENDLIST`, no `#EXT-X-MAP`, no discontinuities.
Unlike the fMP4 playlists of MTS Link it cannot be joined by concatenation into a playable file, but ffmpeg reads it
directly from the URL, which is what `mtsdump` does: one ffmpeg pass per video, download and mp3 encode together.

## Serving quirks

The delivery hosts sign each link for the agent the metadata request used: the path carries `srcAg/CHROME_MAC` when a
Chrome UA fetched the metadata, and a request without a browser User-Agent is answered with HTTP 400 or a tiny non
playlist body. The signature carries an expiry, observed at about 5 days, long after any sensible run.

## Audio

The AAC track is stereo 44.1 kHz even in the cheapest rendition, with the audible content sitting unevenly in the two
channels. Folding stereo to mono lifts peaks by about 3 dB, which is why the fold runs inside the filter chain before
the limiter: as an ffmpeg output option it would be applied after the limiting and undo it.
