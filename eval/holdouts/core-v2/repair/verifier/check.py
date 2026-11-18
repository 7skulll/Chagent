import copy, runpy
def verify(artifacts):
    # Candidate executes only in this disposable, offline verifier container.
    try:
        f=runpy.run_path(str(artifacts/'intervals.py'))['merge_intervals']
        tests=[([],[]), ([[5,7],[1,3],[3,6]],[[1,7]]), ([[1,10],[2,3],[4,6]],[[1,10]]), ([[-5,-2],[-2,0],[8,8],[8,9]],[[-5,0],[8,9]]), ([(4,4),(1,2),(1,2)],[[1,2],[4,4]])]
        for data,expected in tests:
            before=copy.deepcopy(data)
            if f(data)!=expected or data!=before: return False
        for invalid in ([[2,1]],[[1]],[[1,2,3]],[[True,2]],[[1,'2']]):
            try: f(invalid)
            except ValueError: pass
            else: return False
        return True
    except Exception: return False
