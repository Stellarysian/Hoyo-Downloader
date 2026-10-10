# Hoyo Game Downloader

English | [中文](README.zh-CN.md) | [Русский](README.ru-RU.md)

I put this together to download Hoyo games. It's mostly for me, but I
figured I'd throw it up here too in case I need it somewhere else. It's
not done yet, still working on it.

Only really tested it on Genshin, and not that thoroughly either. The
other games, and honestly a lot of the code, I haven't actually tried.
So it might break on you. Can't really promise anything about it.

My code isn't great either. If you spot something weird, that's
probably just me.

If it helps you out, nice, feel free to star it. If something's broken,
open an issue and I'll try to take a look. It's a small thing I made for
myself, so no promises.

## Requirements

Python 3.14 or 3.15. That's what I tested on. On 3.15 one of the
dependencies might need to be built by hand. Not sure about other
versions.

## Install

    pip install -r requirements.txt

## Run

    python main.py

It'll ask you to pick a language the first time.

## Menu

1. Download game
2. Repair / update game
3. View versions
4. View packages
5. Change language
6. Exit

## Notes

Downloads go wherever you want, default is ./<game>.
If it stops partway, just run it again.
The official APIs can change anytime and break this.

## Known Risks

It might grab the wrong game, region, or version.
The version it finds might not match what you picked.
Can't promise it works, or that it's right.

## License

Personal use. Experimental. At your own risk.