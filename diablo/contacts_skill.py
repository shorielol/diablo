import subprocess

def search_contact(name: str):
    """Searches the macOS Address Book (Contacts.app) for a specific name."""
    script = f'''
    tell application "Contacts"
        set matchingPeople to every person whose name contains "{name}"
        if (count of matchingPeople) is 0 then
            return "No contact found for {name}"
        else
            set foundPerson to item 1 of matchingPeople
            set contactName to name of foundPerson
            set contactPhone to value of first phone of foundPerson
            return contactName & ": " & contactPhone
        end if
    end tell
    '''
    try:
        result = subprocess.check_output(["osascript", "-e", script]).decode().strip()
        return result
    except:
        return f"I couldn't access your contacts, Sir. You may need to grant permission."

def get_email(name: str):
    """Retrieves the first email address for a contact."""
    script = f'''
    tell application "Contacts"
        set matchingPeople to every person whose name contains "{name}"
        if (count of matchingPeople) > 0 then
            set foundPerson to item 1 of matchingPeople
            return value of first email of foundPerson
        end if
    end tell
    '''
    try:
        return subprocess.check_output(["osascript", "-e", script]).decode().strip()
    except:
        return None
