
"""When to use: search YouTube for something (e.g. 'search youtube for lofi')."""
def run(ctx, query):
    import urllib.parse
    url = 'https://www.youtube.com/results?search_query=' + urllib.parse.quote(query)
    return ctx.call('open_url', url=url)
