def in_network_urls(index):
    return [
        file["location"]
        for structure in index["reporting_structure"]
        for file in structure["in_network_files"]
    ]
