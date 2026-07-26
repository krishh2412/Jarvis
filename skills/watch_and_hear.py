
"""When to use: know what is going on in whatever video or media is playing - look at the screen and listen to the audio together, then report both."""
def run(ctx, listen_seconds=8, question='What is happening on screen right now?'):
    seen = ctx.call('see_screen', question=question)
    heard = ctx.call('hear_audio', seconds=listen_seconds)
    return {
        'on_screen': seen.get('sees') if isinstance(seen, dict) else seen,
        'heard': heard.get('heard') if isinstance(heard, dict) else heard,
    }
