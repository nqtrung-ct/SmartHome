from pathlib import Path
import subprocess
import yaml

path = Path('/Volumes/Data/frigate-return/config/config.yml')
route = subprocess.check_output(['route', '-n', 'get', 'default'], text=True)
interface = next(line.split()[-1] for line in route.splitlines() if 'interface:' in line)
address = subprocess.check_output(['ipconfig', 'getifaddr', interface], text=True).strip()
config = yaml.safe_load(path.read_text(encoding='utf-8'))
config.setdefault('go2rtc', {}).setdefault('webrtc', {})['candidates'] = [f'{address}:8555']
path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
path.chmod(0o600)
