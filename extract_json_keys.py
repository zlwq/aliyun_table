#!/usr/bin/env python3
import json
import sys


def collect_keys(obj, seen, keys):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in seen:
                seen.add(key)
                keys.append(key)
            collect_keys(value, seen, keys)

    elif isinstance(obj, list):
        for item in obj:
            collect_keys(item, seen, keys)


def main():
    if len(sys.argv) != 2:
        print("用法: python extract_json_keys.py 文件.json")
        sys.exit(1)

    json_file = sys.argv[1]

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = []
    seen = set()
    collect_keys(data, seen, keys)

    print(f"共发现 {len(keys)} 个不同属性：")
    for key in keys:
        print(key)


if __name__ == "__main__":
    main()
