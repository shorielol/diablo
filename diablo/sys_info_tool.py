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
        
    # Disk. The free figure matters more than the percentage — "how much
    # space have I got left" is the question people actually ask, and it was
    # unanswerable from here: the percentage was collected and then only
    # mentioned if it went over ninety, so an agent asked about disk space
    # correctly reported that it had no way to find out.
    disk = psutil.disk_usage("/")
    status["disk_percent"] = disk.percent
    status["disk_free_gb"] = round(disk.free / 1e9, 1)
    status["disk_total_gb"] = round(disk.total / 1e9, 1)

    return status

def format_status_report() -> str:
    stats = get_system_status()
    report = f"Sir, the system is performing within normal parameters. "
    report += f"CPU usage is at {stats['cpu_percent']} percent. "
    report += f"RAM is at {stats['ram_percent']} percent utilized. "
    
    if stats["battery_percent"] is not None:
        plugged = "and charging" if stats["battery_plugged"] else "on battery power"
        report += f"Your battery is at {stats['battery_percent']} percent {plugged}. "
        
    free = stats.get("disk_free_gb")
    if free is not None:
        report += (f"There is {free} gigabytes free of "
                   f"{stats.get('disk_total_gb')} on the disk. ")
    if stats["disk_percent"] > 90:
        report += "Disk space is critically low, Sir, at over 90 percent full."

    return report

if __name__ == "__main__":
    print(format_status_report())
