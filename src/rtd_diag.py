r"""
rtd_diag.py — Run this standalone to diagnose TOS RTD connectivity.
Usage: .venv\Scripts\python src\rtd_diag.py
"""
import winreg, sys, time

def check_registry():
    print("=== Registry ===")
    try:
        key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'tos.rtd')
        print("  HKCR\\tos.rtd  — FOUND")
    except FileNotFoundError:
        print("  HKCR\\tos.rtd  — NOT FOUND (TOS not installed?)")
        return

    # Read CLSID
    try:
        clsid_key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'tos.rtd\CLSID')
        clsid, _ = winreg.QueryValueEx(clsid_key, '')
        print(f"  CLSID: {clsid}")
    except Exception as e:
        print(f"  CLSID: ERROR {e}")
        return

    # Check server type
    base = rf'CLSID\{clsid}'
    for server_type in ('InprocServer32', 'LocalServer32'):
        try:
            sk = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf'{base}\{server_type}')
            path, _ = winreg.QueryValueEx(sk, '')
            print(f"  {server_type}: {path}")
        except FileNotFoundError:
            print(f"  {server_type}: not present")

    # Check if registered in ROT (means TOS already activated it)
    print()

def check_rot():
    print("=== Running Object Table ===")
    import pythoncom, win32com.client
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        obj = win32com.client.GetActiveObject('tos.rtd')
        print("  GetActiveObject('tos.rtd')  — SUCCESS (already in ROT)")
        print("  >> RTD server is primed — GetActiveObject will work")
        return True
    except Exception as e:
        print(f"  GetActiveObject('tos.rtd')  — FAILED: {e}")
        print("  >> Server not in ROT (Excel not open, or TOS RTD not enabled in settings)")
        return False

def check_dispatch():
    print()
    print("=== Dispatch (CoCreateInstance) ===")
    import pythoncom, win32com.client, win32com.server.util
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        rtd = win32com.client.Dispatch('tos.rtd')
        print("  Dispatch('tos.rtd')  — SUCCESS")
        print("  Attempting ServerStart with minimal callback...")

        class CB:
            _public_methods_ = ['UpdateNotify']
            _com_interfaces_ = [pythoncom.IID_IDispatch]
            def UpdateNotify(self): pass

        cb = win32com.server.util.wrap(CB())
        result = rtd.ServerStart(cb)
        print(f"  ServerStart returned: {result}  (1=OK, anything else=problem)")
        if result == 1:
            print("  >> Dispatch + ServerStart WORKS — connection possible without Excel")
        else:
            print("  >> ServerStart failed — may need Excel or TOS setting")
        return result == 1
    except Exception as e:
        print(f"  Dispatch FAILED: {e}")
        if 'InprocServer32' in str(e) or '80040154' in str(e):
            print("  >> Server is InprocServer32 only — cannot CoCreateInstance from outside TOS")
            print("     Only GetActiveObject (after Excel/TOS-setting primes it) will work")
        return False

if __name__ == '__main__':
    check_registry()
    in_rot = check_rot()
    if not in_rot:
        check_dispatch()

    print()
    print("=== Summary ===")
    print("If InprocServer32 only → TOS RTD can only be accessed via GetActiveObject")
    print("  Fix: In TOS go to Setup → Application Settings → Enable RTD Server")
    print("  That makes TOS register the server in the ROT at startup (no Excel needed)")
    print()
    print("If Dispatch worked → we can connect without Excel, code issue to debug")
