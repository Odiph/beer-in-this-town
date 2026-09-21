import sys,time,io,logging
logging.basicConfig(level=logging.ERROR)
from beer_in_this_town.adb_device import AdbDevice
from beer_in_this_town.app_sweep import Cell, sweep, search_city, journal_path
from beer_in_this_town.app_map import pins_in
city=sys.argv[1]; dev=AdbDevice(serial="127.0.0.1:5555")
import os
for d in (0,1):
    try: os.remove(journal_path(city))
    except OSError: pass
    t0=time.time(); search_city(dev,city)
    out=sweep(dev, Cell(0,1,0,1), max_depth=d, verify_pans=True,
              city=city, filter_drinking=True)
    print(f"{city}|depth{d}|cells={out.cells_visited}|venues={len(out.venues)}"
          f"|trunc={out.truncated_cells}|skip={out.skipped_cells}|t={time.time()-t0:.0f}s", flush=True)
    io.open(rf"C:\Users\PANETH~1\AppData\Local\Temp\claude\C--projects-beer-in-this-town\a3c47e79-7bca-4800-a87b-0e0f178b83d5\scratchpad\d{d}_{city.replace(' ','_')}.txt","w",encoding="utf-8").write("\n".join(v.name for v in out.venues))
