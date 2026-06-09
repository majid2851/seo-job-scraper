GEO_BLACKLIST = [
    # US restrictions
    "us only", "usa only", "u.s. only", "united states only",
    "us residents only", "must reside in us", "must be located in the us",
    "must be based in the us", "based in the united states",
    "authorized to work in the us", "authorized to work in the united states",
    "legally authorized to work in the u.s", "work authorization in the us",
    "us citizen", "us citizenship", "green card",
    "must live in the us", "must be in the us",
    "remote - us", "remote (us)", "remote us only", "us remote only",

    # UK / EU / other regions
    "uk only", "united kingdom only", "must be based in the uk",
    "eu only", "europe only", "eea only", "schengen only",
    "canada only", "must be located in canada",
    "australia only", "must be in australia",
    "india only", "must be based in india",

    # Generic location locks
    "must be based in", "must be located in", "must reside in",
    "must live in", "candidates must be located",
    "geographic restriction", "location restriction",
    "within commuting distance", "on-site required", "hybrid only",
    "relocation required", "no relocation",

    # Timezone / region locks (often mean not worldwide)
    "pst only", "est only", "cet only",
    "overlap with us", "us business hours required",
    "within 3 hours of", "same timezone as",
]
