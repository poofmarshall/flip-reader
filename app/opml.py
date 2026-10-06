"""Read an OPML export (NewsBlur, Feedly, any reader). Folders become sections."""
import xml.etree.ElementTree as ET


def parse_opml(data):
    """Returns a list of (section_name, feed_url, title)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("That file isn't a readable OPML export.")
    body = root.find("body")
    if body is None:
        raise ValueError("That file isn't a readable OPML export.")
    out = []

    def walk(node, folder):
        for o in node.findall("outline"):
            url = o.get("xmlUrl") or o.get("xmlurl")
            name = o.get("title") or o.get("text") or ""
            if url:
                out.append((folder or "Unsorted", url.strip(), name.strip() or None))
            else:
                # a folder; nested folders flatten to their top-level name
                walk(o, folder or name.strip() or "Unsorted")

    walk(body, None)
    # NewsBlur can list a feed both loose at the top level and inside a folder;
    # put folder entries first so the folder wins when the importer de-duplicates.
    out.sort(key=lambda item: item[0] == "Unsorted")
    return out
