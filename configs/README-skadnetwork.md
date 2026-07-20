# SKAdNetwork ad network ids

`skadnetwork_ids.plist` (path: `skadnetwork_ids_path`) is the **consolidated
SKAdNetworkItems list downloaded from the MMP dashboard** (Tenjin → the same list is
published by AppsFlyer/Adjust). It is data, not code: networks are added and rotated
upstream, and public community registries go stale.

The build merges it into `ios/Runner/Info.plist`. **A network whose
`SKAdNetworkIdentifier` is missing gets zero SKAN attribution** until the app ships
again — which is why the list should be broad, covering networks you may only start
buying later. With it in place a new traffic source is connected in the Tenjin
dashboard alone, with no rebuild.

Refresh: download the current plist from the dashboard and overwrite this file.
No file present → the merge step is a no-op and only ATT-consenting users attribute.
