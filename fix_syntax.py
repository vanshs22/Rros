import os
import re

def fix_union_types_in_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Replace Optional[str] with Optional[str]
    content = re.sub(r'\bstr\s*\|\s*None\b', 'Optional[str]', content)
    # Replace Optional[int] with Optional[int]
    content = re.sub(r'\bint\s*\|\s*None\b', 'Optional[int]', content)
    # Replace Optional[float] with Optional[float]
    content = re.sub(r'\bfloat\s*\|\s*None\b', 'Optional[float]', content)
    # Replace Optional[bool] with Optional[bool]
    content = re.sub(r'\bbool\s*\|\s*None\b', 'Optional[bool]', content)
    # Replace Optional[dict] with Optional[dict]
    content = re.sub(r'\bdict\s*\|\s*None\b', 'Optional[dict]', content)
    # Replace Optional[list] with Optional[list]
    content = re.sub(r'\blist\s*\|\s*None\b', 'Optional[list]', content)
    # Replace Optional[tuple] with Optional[tuple]
    content = re.sub(r'\btuple\s*\|\s*None\b', 'Optional[tuple]', content)
    
    # Also need to add Optional import if not present
    if 'Optional[' in content and 'from typing import' in content:
        if 'Optional' not in content.split('from typing import')[1].split('\n')[0]:
            # Add Optional to existing typing import
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('from typing import'):
                    if 'Optional' not in line:
                        lines[i] = line.replace('from typing import', 'from typing import Optional,')
                        break
            content = '\n'.join(lines)
    elif 'Optional[' in content and 'from typing import' not in content:
        # Add typing import at the beginning of file
        lines = content.split('\n')
        inserted = False
        for i, line in enumerate(lines):
            if line.startswith('import ') or line.startswith('from '):
                # Insert after the last import
                continue
            else:
                lines.insert(i, 'from typing import Optional')
                inserted = True
                break
        if not inserted:
            lines.insert(0, 'from typing import Optional')
        content = '\n'.join(lines)
    
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"Fixed {filepath}")

def main():
    # Fix all Python files in app directory
    for root, dirs, files in os.walk('.'):
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                fix_union_types_in_file(filepath)

if __name__ == '__main__':
    main()
