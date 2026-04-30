"""Browser and URL control."""

import logging
import subprocess
import webbrowser

logger = logging.getLogger('JARVIS.BrowserControl')


def open_url(url: str, browser: str = None):
    """Open URL in default or specified browser."""
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        if browser:
            subprocess.Popen([browser, url])
        else:
            webbrowser.open(url)
        logger.info(f"Opened URL: {url}")
    except Exception as e:
        logger.error(f"Failed to open URL {url}: {e}")


def open_urls(urls: list):
    """Open multiple URLs (each in a new tab)."""
    for url in urls:
        open_url(url)


def search_google(query: str):
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    open_url(url)


def search_youtube(query: str):
    url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    open_url(url)
