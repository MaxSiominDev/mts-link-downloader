# The MTS Link recording API

Everything here was found by watching what the web player does and by reading the answers it gets. Nothing is official
and nothing is documented by MTS Link, so any of it can change without warning.

## The link

```
https://my.mts-link.ru/j/<organization>/<eventId>/record-new/<eventSessionId>
```

Only the last number is needed, and it is the event session id. The middle number is the event, and it is not used for
anything. The host matters, since an organization can sit on its own subdomain, so it is taken from the link instead of
being hardcoded.

## Metadata

```
GET https://<host>/api/eventsessions/<eventSessionId>/record
```

A public recording answers 200 with no authentication at all. A recording open to participants answers 403, and, oddly,
it keeps answering 403 on this host even with valid session cookies. The cookies are checked by the API gateway of the
same second level domain:

```
GET https://gw.mts-link.ru/api/eventsessions/<eventSessionId>/record
Cookie: <the browser session cookies for .mts-link.ru>
```

A missing recording answers 404.

### The response

```jsonc
{
  // "id" is the record, "eventSession.id" is the number in the link
  "id": <recordId>,
  "eventSession": { "id": <eventSessionId> },
  "name": "Team weekly 2026-09-24 13:59:13",
  "duration": 10562.0,                // seconds from the start to the stop of the session
  "cuts": [],                         // edits made in the MTS Link editor
  "convertedRecords": [],             // a rendered single file, when someone asked for one
  "eventLogs": [ ... ]                // everything that happened, in order
}
```

The two ids are the trap: `id` is the record and `eventSession.id` is what the link, the player and the user call the
recording. Logging the wrong one produces numbers nobody can match to anything.

The title very often ends with the start timestamp as `YYYY-MM-DD HH:MM:SS`, which is why the file naming moves that to
the front instead of duplicating the date.

`duration` is the length of the session, not of its audio. A room that nobody closed keeps recording silence, and a two
hour meeting can carry a three hour `duration`.

### eventLogs

Every entry has `module`, `time` (unix seconds), `relativeTime` (seconds from the start of the session) and `data`. The
modules that matter:

| module | what it carries |
| --- | --- |
| `eventsession.start` | the first entry, its `time` is the start; `snapshot.data.conference` lists the conferences already live at that moment |
| `eventsession.stop` | the end of the session |
| `conference.add`, `conference.update`, `conference.delete` | a participant slot: `data.id` is the conference id, `data.user.id` and `data.user.nickname` are the person |
| `mediasession.add` | a stream appears: `data.id`, `data.url` (mp4), `data.hlsUrl` (master playlist), `data.stream.conference.id`, and `relativeTime` is where it belongs on the timeline |
| `mediasession.update` | carries `data.duration` for a stream that has finished |
| `screensharing.*` | screen sharing, whose streams have no conference and therefore no speaker |

A recording is the join of these: the streams are the `mediasession.add` events, their lengths come from the
`mediasession.update` events with the same id, and their owner comes from the conference the stream belongs to. A
stream still running when the session stopped never gets an `update`, so its duration is unknown until it is probed.

One person can hold several conferences over a meeting, one per reconnect, and can change the nickname between them, so
speakers are collapsed by user id and the last nickname wins.

## The media

Each stream has two addresses. `url` is the full mp4 of that participant, video included. `hlsUrl` is a master playlist
of the same file, served by something that behaves like nginx-vod-module:

```
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="a1",URI="a1/index.m3u8"
```

The audio rendition is a normal fMP4 playlist: one `#EXT-X-MAP` with the initialization section, a list of `.m4s`
segments and `#EXT-X-ENDLIST`. The initialization section followed by its segments, concatenated byte for byte, is a
valid fragmented mp4 that ffmpeg reads without complaint. On a real meeting this matched the audio track of the full
mp4 to the millisecond and cost 6.6 times less traffic.

The playlists have three quirks.

**The EXTINF sum is short.** It understates the decoded length by up to about a second, so it is fine for a progress
bar and useless as a duration. The real length comes from `ffprobe`, and from the first audio stream rather than from
the container, because the container of a camera stream is as long as its video.

**Some playlists carry a discontinuity.** A handful of streams in a large meeting contain `#EXT-X-DISCONTINUITY` and a
second `#EXT-X-MAP`. Those cannot be concatenated into one file, and on such a stream the audio is taken from the full
mp4 instead. It was 4 streams out of 102 in the meeting where this first showed up.

**A master playlist without an audio rendition means one of two things.** Screen sharing has no audio at all and is
skipped. A stream whose master mentions `mp4a` has audio muxed into the video variant only, and the mp4 is used.

## Timing

`relativeTime` of `mediasession.add` is the offset of the stream from the start of the session, and it is what the
player uses to place it. Laying every stream at its offset and summing reproduces the meeting.

Two effects make a naive sum wrong. A stream can contain holes in its timestamps, left by a reconnect, and a decoder
that ignores them shifts everything after the hole earlier, which desynchronizes that participant for the rest of the
meeting. And plenty of streams are digital silence from beginning to end, because a muted microphone is still recorded:
in a 36 stream meeting, 11 of them contained nothing but zeros.

## Cookies, if you want to do this yourself

Chrome on macOS keeps its cookies in `~/Library/Application Support/Google/Chrome/<profile>/Cookies`, an sqlite
database it holds open, so it has to be copied together with its journal before reading. Values are encrypted:

- the prefix is `v10`
- the key is PBKDF2-HMAC-SHA1 over the Keychain secret stored under the service `Chrome Safe Storage`, with the salt
  `saltysalt`, 1003 iterations and a 16 byte output
- the cipher is AES-128-CBC with an IV of 16 spaces
- from cookie database version 24 onward the decrypted value starts with a 32 byte SHA-256 of the cookie host key,
  which has to be stripped
- expiry timestamps are microseconds since 1601

Reading the Keychain secret asks the user once per application.

## Serving quirks

Segment requests are answered by a CDN that occasionally drops a connection in the middle of a response. `Range` is
supported, so a half-downloaded mp4 resumes instead of starting over; a segment is small enough to just refetch. Note that `http.client` returns an empty read on a
dropped connection instead of raising, which looks exactly like a finished download unless the remaining length is
checked.
