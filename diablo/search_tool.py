import webbrowser
import urllib.parse
from typing import Optional

def google_search(query: str):
    """
    Opens the default web browser with Google Search results.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={encoded_query}"
    webbrowser.open(url)
    return f"Searching for {query} on Google, Sir."

def find_news(query: str):
    """
    Searches specifically in Google News.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={encoded_query}&tbm=nws"
    webbrowser.open(url)
    return f"Finding latest articles on {query}, Sir."

def play_latest_video(channel_query: str):
    """
    Finds the latest video from a channel using YouTube filters.
    sp=CAI%253D is the filter for 'Upload Date'
    """
    encoded_query = urllib.parse.quote(channel_query)
    # This URL searches and applies the 'Upload date' filter automatically
    url = f"https://www.youtube.com/results?search_query={encoded_query}&sp=CAI%253D"
    webbrowser.open(url)
    return f"Finding the latest video for {channel_query}, Sir."

def search_youtube(query: str):
    """
    Opens YouTube search results for a specific query.
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.youtube.com/results?search_query={encoded_query}"
    webbrowser.open(url)
    return f"Searching YouTube for {query}, Sir."

if __name__ == "__main__":
    # Test
    play_latest_video("Wembu")
