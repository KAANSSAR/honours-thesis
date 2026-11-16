"""Level 1: does every number in Chapter 4 reconcile with the others?

Pure arithmetic. Catches transcription slips, which is the most likely error
when a few dozen figures are retyped by hand.
"""
ok = bad = 0
def chk(label, got, want, tol=0.05):
    global ok, bad
    good = abs(got - want) <= tol
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got} vs {want}")
    globals().__setitem__('ok', ok + good); globals().__setitem__('bad', bad + (not good))

print("Table 4.3 rows must sum to the scenario counts")
for name, syd, uts in (("Neither", (2,485,731), (0,57,222)),
                       ("Compaction only", (836,260,122), (1,135,143)),
                       ("Overload in span only", (722,244,252), (114,42,123)),
                       ("Both, as built", (894,231,93), (115,85,79))):
    chk(f"{name} USYD", sum(syd), 1218, 0)
    chk(f"{name} UTS", sum(uts), 279, 0)

print("\n1,218 + 279 = 1,497 scenarios")
chk("scenario total", 1218+279, 1497, 0)

print("\nOptimality")
chk("judged = optimal + shorter", 1299+84, 1383, 0)
chk("judged = 1497 - notmodelled - badwitness", 1497-11-103, 1383, 0)
chk("optimal percent", 100*1299/1383, 93.9, 0.05)
chk("USYD judged", 1137+23, 1160, 0)
chk("USYD percent", 100*1137/1160, 98.0, 0.05)
chk("UTS judged", 162+61, 223, 0)
chk("UTS percent", 100*162/223, 72.6, 0.05)
chk("institution judged sums to total", 1160+223, 1383, 0)
chk("shorter sums", 23+61, 84, 0)

print("\nDifferential")
chk("agree + disagree", 551+49, 600, 0)
chk("both legal + both illegal", 192+359, 551, 0)
chk("stricter split", 35+14, 49, 0)
chk("agreement percent", 100*551/600, 91.8, 0.05)
chk("engine-stricter rule tally >= cases", 22+13+5+1 >= 35, True, 0)

print("\nCoverage figures")
chk("Sydney uncovered percent", 100*133/217, 61, 0.5)
chk("UTS uncovered percent", 100*51/217, 24, 0.5)
chk("ch5 Sydney covered percent", 100*(217-133)/217, 39, 0.5)
chk("ch5 UTS covered percent", 100*(217-51)/217, 76, 0.5)

print(f"\n{ok} passed, {bad} failed")
