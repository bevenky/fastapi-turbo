"""Helper for running async handlers on the persistent event loop."""

import asyncio


def schedule_on_loop(handler, kwargs_dict, event_loop, callback):
    """Schedule handler(**kwargs) on the event loop and call callback(result, error) when done."""

    async def _wrapper():
        return await handler(**kwargs_dict)

    future = asyncio.run_coroutine_threadsafe(_wrapper(), event_loop)
    future.add_done_callback(lambda f: callback(f))
