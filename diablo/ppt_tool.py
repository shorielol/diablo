import os
import random
from pptx import Presentation
# The shipped module, installed beside this one. `from tools import llm_tool`
# only resolved on a machine that still had a legacy ~/.diablo/tools folder,
# and there it ran an older copy than the one in the repository.
from skills import llm_tool

def create_presentation(topic: str, slide_count: int = 8):
    """
    Generates a professional PowerPoint presentation on the given topic.
    Uses LLM to generate the structure and content.
    """
    print(f"  [PPT Tool] Generating presentation for topic: {topic}...")
    
    # Define the prompt for the LLM
    prompt = f"""
    Create a detailed structure for a PowerPoint presentation about "{topic}".
    Return the response in a parseable format strictly following this example:
    
    #Title: [Presentation Title]
    
    #Slide: 1
    #Header: Introduction
    #Content: [Detailed content for the introduction slide...]
    
    ...
    
    #Slide: {slide_count}
    #Header: Conclusion
    #Content: [Summary and conclusion...]
    
    #Slide: END
    
    Ensure exactly {slide_count} slides are generated. Be professional and detailed.
    """
    
    # Generate content using the existing LLM tool
    llm_response = llm_tool.ask_diablo_ai(prompt)
    
    # Path for saving
    output_dir = os.path.expanduser("~/Desktop/Presentations")
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{topic.replace(' ', '_')}_{int(random.random()*1000)}.pptx"
    output_path = os.path.join(output_dir, filename)
    
    # Create the presentation
    prs = Presentation()
    
    # Parse the LLM response
    lines = llm_response.split("\n")
    current_title = ""
    current_header = ""
    current_content = ""
    
    for line in lines:
        if line.startswith("#Title:"):
            current_title = line.replace("#Title:", "").strip()
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = current_title
        elif line.startswith("#Slide:"):
            if current_header and current_content:
                # Add the previous slide before clearing
                slide = prs.slides.add_slide(prs.slide_layouts[1])
                slide.shapes.title.text = current_header
                slide.placeholders[1].text = current_content
                current_header = ""
                current_content = ""
        elif line.startswith("#Header:"):
            current_header = line.replace("#Header:", "").strip()
        elif line.startswith("#Content:"):
            current_content = line.replace("#Content:", "").strip()
        elif not line.startswith("#") and current_content:
            current_content += "\n" + line.strip()

    prs.save(output_path)
    return f"Presentation on '{topic}' has been saved to your Desktop, Sir."

if __name__ == "__main__":
    # Test
    pass
