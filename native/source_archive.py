"""Archive exact corresponding tracked source; no output/credentials/user data."""
from pathlib import Path
import subprocess,sys,zipfile
root=Path(__file__).resolve().parent.parent
output=Path(sys.argv[1]).resolve()
assert output.suffix=='.zip'
subprocess.run(['git','archive','--format=zip','--output='+str(output),'HEAD'],cwd=root,check=True)

with zipfile.ZipFile(output,"a",zipfile.ZIP_DEFLATED) as archive:
 archive.writestr("SOURCE-REVISION.txt",subprocess.check_output(["git","rev-parse","HEAD"],cwd=root))
