from pathlib import Path
import subprocess
import yaml

route = subprocess.check_output(['route', '-n', 'get', 'default'], text=True)
interface = next(line.split()[-1] for line in route.splitlines() if 'interface:' in line)
address = subprocess.check_output(['ipconfig', 'getifaddr', interface], text=True).strip()
peer = {'192.168.1.101': '192.168.1.99', '192.168.1.99': '192.168.1.101'}.get(address)
if peer is None:
    raise SystemExit('Unexpected Mac IP; Ollama endpoint was not changed')
path = Path('/Volumes/Data/Projects/SmartHome/config.yaml')
config = yaml.safe_load(path.read_text(encoding='utf-8'))
config['llm']['base_url'] = f'http://{peer}:11434'
config['llm']['ollama_url'] = f'http://{peer}:11434'
config['frigate']['config_path'] = '/Volumes/Data/frigate-return/config/config.yml'
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
path.chmod(0o600)
