import json
import sys

# Read notebook
nb_path = r'e:\xwechat_files\wxid_tcciurgq8a7222_c47b\msg\file\2026-03\NewModel (2)\DAH_Front_Back_14input_Delta12_EWA12_6DK2.ipynb'

try:
    with open(nb_path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    fixed = 0
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            source = cell['source']
            code_str = ''.join(source) if isinstance(source, list) else source
            
            if 'build_all_datasets' in code_str:
                # Find and fix indentation issues
                lines = code_str.split('\n')
                new_lines = []
                i = 0
                
                while i < len(lines):
                    line = lines[i]
                    
                    # Fix: continue should be inside if block
                    if i > 0 and 'if n_valid == 0:' in lines[i-1]:
                        if line.strip() == 'continue':
                            new_lines.append('            continue')
                            i += 1
                            continue
                    
                    # Fix: if is_wanyou: block indentation
                    if line.strip() == 'if is_wanyou:':
                        new_lines.append(line)
                        i += 1
                        while i < len(lines) and lines[i].strip() != 'else:':
                            curr = lines[i]
                            if curr.strip():
                                if curr.startswith('        ') and not curr.startswith('            '):
                                    new_lines.append('            ' + curr.lstrip())
                                else:
                                    new_lines.append(curr)
                            else:
                                new_lines.append(curr)
                            i += 1
                        continue
                    
                    # Fix: else block indentation
                    if line.strip() == 'else:':
                        new_lines.append(line)
                        i += 1
                        while i < len(lines):
                            curr = lines[i]
                            if curr and not curr.startswith(' '):
                                break
                            if curr.strip():
                                if curr.startswith('        ') and not curr.startswith('            '):
                                    new_lines.append('            ' + curr.lstrip())
                                else:
                                    new_lines.append(curr)
                            else:
                                new_lines.append(curr)
                            i += 1
                        continue
                    
                    new_lines.append(line)
                    i += 1
                
                # Update cell
                fixed_code = '\n'.join(new_lines)
                cell['source'] = [line + '\n' for line in new_lines[:-1]] + [new_lines[-1]]
                cell['outputs'] = []
                cell['execution_count'] = None
                fixed += 1
    
    # Save
    out_path = nb_path.replace('.ipynb', '_FIXED.ipynb')
    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)
    
    print(f"OK: {out_path}")
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
