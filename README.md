# MTS Link downloader

[![tests](https://github.com/MaxSiominDev/mts-link-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/MaxSiominDev/mts-link-downloader/actions/workflows/tests.yml)

Turns an MTS Link meeting recording into one mp3 with everybody's audio on a single timeline.

## Why

The MTS Link player has no "download audio" button, and the recording behind it is not one file. Every camera and
microphone is a separate media stream that starts and stops when its owner joins, mutes or reconnects, and the player
mixes them live while you watch. A two-hour meeting is routinely 30 to 100 streams, each carrying one person and
starting at its own offset from the beginning of the session.

I wanted our lectures as a single file I could listen to at double speed and hand to a transcriber, so this reads the
recording metadata, downloads the audio of every stream and puts each one back at its offset. It takes only the audio
rendition of each stream, which on a real meeting was 6.6 times less traffic than the mp4s.

## Requirements

- macOS
- Python 3.14 or newer
- ffmpeg and ffprobe in `PATH` (`brew install ffmpeg`), tested on ffmpeg 9

Standard library only, no Python dependencies.

## Install

```sh
pipx install git+https://github.com/MaxSiominDev/mts-link-downloader
```

`uv tool install` from the same URL works too, or clone the repository and run `python3 -m mtsdump` from it.

## Usage

```sh
mtsdump https://my.mts-link.ru/j/Org/11111111111/record-new/22222222222
```

Only the last number of the link matters. A bare session id works when the organization sits on `my.mts-link.ru`,
otherwise pass the whole link, the host is taken from it. Several recordings in one command are fine:

```sh
mtsdump -o ~/Downloads 22222222222 33333333333
```

The name is `<date> <name>.mp3`, taken from the meeting title. `--name` takes `{date}`, `{name}` and `{id}`. An existing
file is neither overwritten nor rebuilt.

A run against a private recording:

```
$ mtsdump --cookies-from-browser chrome --verify -o ~/Downloads 22222222222
22222222222: Team weekly 2026-09-24 13:59:13
36 streams, 35 with audio, 1 without
downloaded 35 streams, 775 MB
mix length 1:55:03, session length 2:56:02
verify: length 1:55:02.9, the streams end at 1:55:02.9
verify: mean -19.8 dB, max -0.6 dB
verify: 15 silences of 2 s or longer in the mix, 0 of them while some stream has sound
verify: 11 streams are digital silence: 111111111, 111111112, ...
saved /Users/you/Downloads/2026-09-24 13-59-13 Team weekly.mp3
```

The parts live in `DIR/<id>.parts` during the run and are deleted at the end unless you pass `--keep`. Two hours of
meeting means a few hundred MB of traffic and a few minutes of ffmpeg once everything is downloaded.

`--list` fetches the playlists and prints the streams without downloading any audio:

```
    stream     start    length  audio  speaker
 111111111   0:00:12   1:54:31  yes    Anna
 111111112   0:00:35   0:00:00  no     -
```

## Private recordings

A recording open to participants only answers 403 on the public endpoint. `mtsdump` then repeats the request to the API
gateway of the same domain with the cookies of your logged-in session, which means it reaches exactly the recordings you
can already open in the player:

```sh
mtsdump --cookies-from-browser chrome 22222222222
```

That reads the cookie database of the local Chrome profile and asks the Keychain once for the key Chrome encrypted the
values with. Pick a profile with `--cookies-from-browser "chrome:Profile 1"`.

If you would rather not give a program Keychain access, copy the `Cookie` header from the devtools network tab of an
open MTS Link page and pass it directly:

```sh
mtsdump --cookie "sessionId=...; ..." 22222222222
```

Either way the cookies go only to `mts-link.ru` and `webinar.ru` hosts, and they never reach the log.

Two failures are common. `rejected the cookies` means the session is over, so open MTS Link in the browser again.
`Chrome has no cookies for gw.mts-link.ru` means that profile has nothing for the domain at all: the session cookie is
set on `.mts-link.ru` and covers the gateway host too, so logging in to MTS Link in that profile is enough.

## Options

| Option | What it does |
| --- | --- |
| `-o`, `--output DIR` | where to save the files, default is the current folder |
| `--name TEMPLATE` | file name from `{date}`, `{name}` and `{id}`, default `{date} {name}` |
| `-b`, `--bitrate RATE` | mp3 bitrate, default 64k, or 32k with `--asr` |
| `--asr` | 16 kHz with a 70 Hz high-pass, for speech recognition instead of listening |
| `--split-speakers` | also save one track per participant, without loudness normalization |
| `--verify` | check the saved mix against the sources |
| `--list` | print the streams and exit |
| `--keep` | keep the downloaded streams in `DIR/<id>.parts` |
| `--mp4` | take the audio from the full mp4 files instead of HLS |
| `--cookie STR` | `Cookie` header copied from the browser |
| `--cookies-from-browser chrome[:PROFILE]` | read the cookies of the local Chrome profile |
| `-j`, `--jobs N` | parallel downloads, default 6 |
| `--version` | print the version and exit |

## How the mix is built

Every stream is decoded to mono 48 kHz float, delayed to its own offset with `adelay` and summed with
`amix=normalize=0`, which is a plain sum: the default `amix` divides every input by the number of inputs, which would
make a 40 person meeting inaudible. Timestamp holes inside a stream, left by a reconnect, are filled with
`aresample=async=1:first_pts=0`, so a stream that says it lasts an hour occupies an hour on the timeline.

The sum is then normalized to -16 LUFS with `loudnorm` and capped by `alimiter` at -3 dBFS. The ceiling is not -1 dBFS
because the mp3 encoder lifts peaks by up to 2 dB at these bitrates, and `alimiter` runs with `level=0`, since its
automatic makeup gain would push everything it just limited back to full scale.

Above 50 streams ffmpeg is given the work in groups of 20, written as lossless WavPack float and summed afterwards.
Those group files are float at 48 kHz, so they take a few GB in the parts folder until the mix is done. Summing in
groups was checked against a single pass on a real 102 stream meeting: the difference peaks at -138 dB.

The result is as long as the last stream, not as long as the session. A meeting whose room was left open for another
hour produces a mix that ends when the last person stopped talking.

## Checking the result

`--verify` decodes the finished mp3 and compares it with the sources:

- the length against the end of the last stream, a drift above 0.5 s is a warning
- mean and peak level
- silences of 2 seconds or longer, and, for each of them, whether any stream had sound of its own at that moment

A silence that no stream explains means audio was lost somewhere. A mix full of silences that every stream agrees with
is just a quiet meeting. Streams that are digital silence from start to end (a participant who never unmuted) are
listed separately, and with `--split-speakers` no track is written for them.

## Known limits

- macOS only
- Cookies are read from Chrome. With any other browser, copy the header and use `--cookie`.
- Only `/record-new/` links are parsed, older recording URLs are not.
- A recording that was cut in the MTS Link editor is assembled whole: the cut parts come back and the timing stops
  matching the player after the first cut. `mtsdump` warns when it sees this.
- A few streams in a large meeting have a playlist that cannot be joined into a single file (two initialization
  sections after a discontinuity). Their audio is taken from the full mp4 instead, which costs extra traffic. The log
  says which streams these were.
- Only audio for now. The video of every stream stays on the server.

## Exit codes

`0` success, `1` something failed, `2` bad arguments, `130` interrupted with Ctrl+C. An interrupted run keeps what it
downloaded in `DIR/<id>.parts` and the next run continues from there.

## Development

```sh
python3 -m unittest
ruff check .
```

The tests that need ffmpeg build their own tone files and are skipped when it is missing.

[docs/mts-link-api.md](docs/mts-link-api.md) is what I found out about the recording API and its HLS streams.

## License

MIT, see [LICENSE](LICENSE).
