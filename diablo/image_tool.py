import requests
import os
import random
import time

def generate_image(prompt: str):
    """
    Generates an image using a public API (Pollinations.ai) for demonstration.
    Saves the image to the Desktop.
    """
    print(f"  [Image Tool] Generating image for: {prompt}...")
    
    # Clean up the prompt for URL
    clean_prompt = prompt.replace(" ", "%20")
    seed = random.randint(1, 100000)
    url = f"https://pollinations.ai/p/{clean_prompt}?width=1024&height=1024&seed={seed}&model=flux"
    
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            output_dir = os.path.expanduser("~/Desktop/Generated_Images")
            os.makedirs(output_dir, exist_ok=True)
            filename = f"DIABLO_Image_{int(time.time())}.png"
            output_path = os.path.join(output_dir, filename)
            
            with open(output_path, "wb") as f:
                f.write(response.content)
            
            return f"I've generated the image of '{prompt}' for you, Sir. It's saved on your Desktop."
        else:
            return "I'm sorry Sir, I encountered an error during image generation."
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    # Test
    # print(generate_image("A futuristic city in the style of Tony Stark"))
    pass
