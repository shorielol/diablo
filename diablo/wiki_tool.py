import wikipedia
from typing import Optional

def search_wikipedia(query: str, sentences: int = 2) -> str:
    """
    Searches Wikipedia for a query and returns a summary.
    """
    try:
        # Set language to English
        wikipedia.set_lang("en")
        # Get summary
        summary = wikipedia.summary(query, sentences=sentences)
        return summary
    except wikipedia.exceptions.DisambiguationError as e:
        # If multiple results, just take the first one or ask for specifics
        return f"Sir, that topic is quite broad. It could refer to {', '.join(e.options[:3])}. Could you be more specific?"
    except wikipedia.exceptions.PageError:
        return f"I'm sorry Sir, I couldn't find any Wikipedia entry for {query}."
    except Exception as e:
        return f"I encountered an error while searching Wikipedia: {str(e)}"

if __name__ == "__main__":
    # Test
    print(search_wikipedia("D.I.A.B.L.O."))
