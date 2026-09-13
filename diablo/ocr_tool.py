import pyautogui
import os
import time
from PIL import Image
try:
    import easyocr
    READER = easyocr.Reader(['en'])
except ImportError:
    READER = None

def capture_and_ocr():
    """Captures a screenshot and performs OCR on the entire screen."""
    print("  [OCR Tool] Capturing screen for OCR...")
    screenshot_path = "temp_ocr.png"
    pyautogui.screenshot(screenshot_path)
    
    if not READER:
        return "Error: easyocr not installed. Please install it to use OCR features."
    
    results = READER.readtext(screenshot_path)
    os.remove(screenshot_path)
    
    # Combine results
    full_text = " ".join([res[1] for res in results])
    return full_text

def find_and_click_text(target_text: str):
    """Finds specific text on screen and clicks it."""
    print(f"  [OCR Tool] Searching for '{target_text}' on screen...")
    screenshot_path = "temp_click.png"
    pyautogui.screenshot(screenshot_path)
    
    if not READER:
        return "Error: easyocr not installed."
        
    results = READER.readtext(screenshot_path)
    os.remove(screenshot_path)
    
    for (bbox, text, prob) in results:
        if target_text.lower() in text.lower():
            # Calculate center of bbox
            # bbox is [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            center_x = (bbox[0][0] + bbox[1][0]) / 2
            center_y = (bbox[0][1] + bbox[2][1]) / 2
            
            print(f"  [OCR Tool] Found '{text}' at ({center_x}, {center_y}). Clicking...")
            pyautogui.click(center_x, center_y)
            return f"Clicked on '{text}', Sir."
            
    return f"Could not find '{target_text}' on screen, Sir."

if __name__ == "__main__":
    # Test
    # print(capture_and_ocr())
    pass
