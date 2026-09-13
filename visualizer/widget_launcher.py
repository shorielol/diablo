import webview
import os
import sys

def run_widget():
    """
    Launches a minimalist, holographic HUD for D.I.A.B.L.O.
    Enforces strict transparency for macOS.
    Increased size to 240x240 to prevent ring clipping.
    """
    try:
        # Core resized to 240x240 for rotating rings
        window = webview.create_window(
            'D.I.A.B.L.O. HUD',
            'http://localhost:8000/index.html',
            width=240,
            height=240,
            x=1200,             
            y=20,               
            frameless=True,      
            easy_drag=True,     
            on_top=True,        
            transparent=True,   
            background_color='#000000'
        )
        
        webview.start(gui='cocoa')
        
    except Exception as e:
        print(f"  [HUD Error] {e}")
        try:
            webview.start()
        except:
            pass

if __name__ == '__main__':
    run_widget()
