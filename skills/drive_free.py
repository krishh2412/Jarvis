
def run(ctx, drive="D:"):
    stats = ctx.call("system_stats")
    for d in stats.get("disks", []):
        if d["drive"].startswith(drive):
            return {"drive": d["drive"], "free_gb": round(d["total_gb"] - d["used_gb"], 1)}
    return {"error": "drive not found"}
