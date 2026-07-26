
"""When to use: ask ChatGPT or Gemini a question in the browser (Sir must be logged in). Types into the composer, which both sites auto-focus on load."""
def run(ctx, question, service='chatgpt'):
    import time
    urls = {'chatgpt': 'https://chatgpt.com', 'gemini': 'https://gemini.google.com/app'}
    ctx.call('open_url', url=urls.get(service, urls['chatgpt']))
    time.sleep(7)  # allow the page to load and focus its input box
    return ctx.call('type_in_field', text=question, submit=True)
