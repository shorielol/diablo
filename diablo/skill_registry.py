import os
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
                    spec = importlib.util.spec_from_file_location(skill_name, module_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
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
