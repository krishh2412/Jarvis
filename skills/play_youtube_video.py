
"""When to use: play a specific YouTube video when you have its URL or 11-char video id."""
def run(ctx, url_or_id):
    v = str(url_or_id).strip()
    if not v.startswith('http'):
        v = 'https://www.youtube.com/watch?v=' + v
    return ctx.call('open_url', url=v)
