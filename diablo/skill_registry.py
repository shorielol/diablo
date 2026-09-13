import os
import importlib
import importlib.util
import sys

SKILLS_DIR = os.path.dirname(os.path.abspath(__file__))

class SkillRegistry:
    def __init__(self):
        self.skills = {}

    def discover_skills(self):
        """Automatically discovers and indexes all Python modules in the skills directory."""
        print("  [Synaptic Core] Discovering Skills...")
        for filename in os.listdir(SKILLS_DIR):
            if filename.endswith(".py") and filename != "__init__.py" and filename != "skill_registry.py":
                skill_name = filename[:-3]
                module_path = os.path.join(SKILLS_DIR, filename)
                
                try:
                    # Import through the package, not from the file path.
                    # spec_from_file_location built a brand new module object
                    # every time, so a skill already imported as skills.x got a
                    # second, independent copy here with its own globals and its
                    # own configuration. Thirty skills, thirty extra copies.
                    module = importlib.import_module(f"skills.{skill_name}")
                    self.skills[skill_name] = module
                    print(f"    ✓ Skill Indexed: {skill_name}")
                except Exception as e:
                    print(f"    ✗ Failed to index {skill_name}: {e}")
        
        return self.skills

    def get_skill(self, name):
        return self.skills.get(name)

# Singleton for easy access
registry = SkillRegistry()

if __name__ == "__main__":
    registry.discover_skills()
