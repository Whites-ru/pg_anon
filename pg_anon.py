import asyncio

from pg_anon import MainRoutine

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(MainRoutine().run())
