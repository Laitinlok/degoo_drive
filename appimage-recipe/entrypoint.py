# AppImage entrypoint — delegates to degoo_gui.main
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from degoo_gui.main import main
main()
