"""Ponto de entrada do APOLO:  python run.py  (ou  python run.py --minimized)"""
if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        from app.packaging_check import main
    else:
        from app.main import main
    main()
