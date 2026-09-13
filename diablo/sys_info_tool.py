import psutil
import platform
import os
from typing import Dict

def get_system_status() -> Dict:
    """
    Returns a dictionary of system health stats.
    """
    status = {}
    
    # CPU
    status["cpu_percent"] = psutil.cpu_percent(interval=0.1)
    
    # RAM
    ram = psutil.virtual_memory()
    status["ram_percent"] = ram.percent
    
    # Battery
    battery = psutil.sensors_battery()
    if battery:
        status["battery_percent"] = battery.percent
        status["battery_plugged"] = battery.power_plugged
    else:
        status["battery_percent"] = None
        
    # Disk
    disk = psutil.disk_usage("/")
    status["disk_percent"] = disk.percent
    
    return status

def format_status_report() -> str:
    stats = get_system_status()
    report = f"Sir, the system is performing within normal parameters. "
    report += f"CPU usage is at {stats['cpu_percent']} percent. "
    report += f"RAM is at {stats['ram_percent']} percent utilized. "
    
    if stats["battery_percent"] is not None:
        plugged = "and charging" if stats["battery_plugged"] else "on battery power"
        report += f"Your battery is at {stats['battery_percent']} percent {plugged}. "
        
    if stats["disk_percent"] > 90:
        report += "Warning Sir, disk space is critically low at over 90 percent capacity."
    
    return report

if __name__ == "__main__":
    print(format_status_report())
