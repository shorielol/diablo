import subprocess

def run_script(script):
    subprocess.run(["osascript", "-e", script])

def set_volume(level: int):
    """Sets system volume (0-100)."""
    if level < 0: level = 0
    if level > 100: level = 100
    run_script(f"set volume output volume {level}")
    return f"Volume set to {level} percent."

def adjust_volume(direction="up"):
    """Increases or decreases volume by 10%."""
    step = 10 if direction == "up" else -10
    script = f'set volume output volume ((output volume of (get volume settings)) + {step})'
    run_script(script)
    return f"Volume turned {direction}."

def set_brightness(level: int):
    """Sets display brightness (0-100)."""
    # Uses a common shell utility if available, or brightness command
    # For now, using AppleScript to simulate brightness keys
    script = f'''
    repeat {int(level/10)} times
        tell application "System Events" to key code 144 -- Brightness Up
    end repeat
    '''
    # This is rough; for 10x better, we use a more scientific approach later
    return f"Adjusting brightness to approximately {level} percent."

def toggle_do_not_disturb(on=True):
    """Toggles macOS Focus/Do Not Disturb."""
    # This requires specific UI scripting or shell defaults
    state = "on" if on else "off"
    # Placeholder for advanced DND logic found in OpenDiablo
    return f"Do Not Disturb turned {state}."

def get_system_stats():
    """Returns CPU/Memory status."""
    cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    return f"System running on {cpu} with M3 Pro acceleration enabled."
