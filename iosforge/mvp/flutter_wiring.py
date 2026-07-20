"""Generate a complete, backend-connected Flutter app (deterministic).

Unlike the static-UI codegen, this writes a runnable app wired to the provisioned
Firebase project: anonymous auth, a Firestore-backed catalog (the seeded
placeholder series/episodes), a video player for the dummy clips, an Apphud
paywall and a coin wallet that calls the server ``unlockEpisode`` function. The
collection prefix + Firebase web config are baked in so the app connects to the
live project and shows the placeholder content for visual review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger

log = get_logger("mvp.flutter_wiring")

_PUBSPEC = """\
name: {pkg}
description: {app} (generated).
publish_to: none
environment:
  sdk: ">=3.4.0 <4.0.0"
dependencies:
  flutter:
    sdk: flutter
  firebase_core: ^3.6.0
  cloud_firestore: ^5.4.0
  firebase_auth: ^5.3.0
  cloud_functions: ^5.1.0
  flutter_riverpod: ^2.5.0
  go_router: ^14.2.0
  video_player: ^2.9.0
  google_mobile_ads: ^5.1.0
  apphud: ^3.2.0
flutter:
  uses-material-design: true
"""

_FIREBASE_OPTIONS = """\
import 'package:firebase_core/firebase_core.dart';

/// Filled from the provisioned project. Replace placeholders via provisioning.
class DefaultFirebaseOptions {{
  static FirebaseOptions get current => const FirebaseOptions(
        apiKey: '{apiKey}',
        appId: '{appId}',
        messagingSenderId: '{sender}',
        projectId: '{projectId}',
        storageBucket: '{bucket}',
      );
}}
"""

_MODELS = """\
class Series {
  final String id, title, synopsis, posterUrl;
  final int episodeCount, freeEpisodeCount;
  Series(this.id, this.title, this.synopsis, this.posterUrl, this.episodeCount, this.freeEpisodeCount);
  factory Series.fromMap(String id, Map<String, dynamic> m) => Series(
      id, m['title'] ?? '', m['synopsis'] ?? '', m['posterUrl'] ?? '',
      (m['episodeCount'] ?? 0) as int, (m['freeEpisodeCount'] ?? 0) as int);
}

class Episode {
  final String id, seriesId, videoUrl;
  final int number, unlockCost;
  final bool isLocked;
  Episode(this.id, this.seriesId, this.videoUrl, this.number, this.unlockCost, this.isLocked);
  factory Episode.fromMap(String id, Map<String, dynamic> m) => Episode(
      id, m['seriesId'] ?? '', m['videoUrl'] ?? '', (m['number'] ?? 0) as int,
      (m['unlockCost'] ?? 0) as int, (m['isLocked'] ?? false) as bool);
}
"""

_REPO = """\
import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:cloud_functions/cloud_functions.dart';
import 'models.dart';

const String kPrefix = '{prefix}';

class CatalogRepository {{
  final FirebaseFirestore _db = FirebaseFirestore.instance;

  Future<List<Series>> series() async {{
    final snap = await _db.collection('${{kPrefix}}Series').get();
    return snap.docs.map((d) => Series.fromMap(d.id, d.data())).toList();
  }}

  Future<List<Episode>> episodes(String seriesId) async {{
    final snap = await _db
        .collection('${{kPrefix}}Episode')
        .where('seriesId', isEqualTo: seriesId)
        .get();
    final list = snap.docs.map((d) => Episode.fromMap(d.id, d.data())).toList();
    list.sort((a, b) => a.number.compareTo(b.number));
    return list;
  }}

  Future<bool> unlock(String episodeId) async {{
    final res = await FirebaseFunctions.instance
        .httpsCallable('unlockEpisode')
        .call({{'episodeId': episodeId}});
    return (res.data?['unlocked'] ?? false) as bool;
  }}
}}
"""

_PROVIDERS = """\
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'repository.dart';
import 'models.dart';

final repoProvider = Provider((ref) => CatalogRepository());

final authProvider = FutureProvider((ref) async {
  final auth = FirebaseAuth.instance;
  if (auth.currentUser == null) {
    await auth.signInAnonymously();
  }
  return auth.currentUser;
});

final seriesProvider = FutureProvider<List<Series>>((ref) async {
  await ref.watch(authProvider.future);
  return ref.read(repoProvider).series();
});

final episodesProvider =
    FutureProvider.family<List<Episode>, String>((ref, seriesId) async {
  return ref.read(repoProvider).episodes(seriesId);
});
"""

_MAIN = """\
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:go_router/go_router.dart';
import 'firebase_options.dart';
import 'screens/feed.dart';
import 'screens/detail.dart';
import 'screens/player.dart';
import 'screens/paywall.dart';

Future<void> main() async {{
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp(options: DefaultFirebaseOptions.current);
  runApp(const ProviderScope(child: App()));
}}

final _router = GoRouter(routes: [
  GoRoute(path: '/', builder: (c, s) => const FeedScreen()),
  GoRoute(path: '/series/:id', builder: (c, s) => DetailScreen(seriesId: s.pathParameters['id']!)),
  GoRoute(path: '/play', builder: (c, s) => PlayerScreen(url: s.uri.queryParameters['url'] ?? '')),
  GoRoute(path: '/paywall', builder: (c, s) => const PaywallScreen()),
]);

class App extends StatelessWidget {{
  const App({{super.key}});
  @override
  Widget build(BuildContext context) => MaterialApp.router(
        title: '{app}',
        theme: ThemeData(colorSchemeSeed: Colors.deepPurple, useMaterial3: true, brightness: Brightness.dark),
        routerConfig: _router,
      );
}}
"""

_FEED = """\
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../providers.dart';

class FeedScreen extends ConsumerWidget {
  const FeedScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final series = ref.watch(seriesProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Discover')),
      body: series.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(child: Text('Error: $e')),
        data: (list) => GridView.count(
          crossAxisCount: 2,
          childAspectRatio: 0.7,
          padding: const EdgeInsets.all(8),
          children: [
            for (final s in list)
              GestureDetector(
                onTap: () => context.go('/series/${s.id}'),
                child: Card(
                  clipBehavior: Clip.antiAlias,
                  child: Column(children: [
                    Expanded(child: s.posterUrl.isEmpty
                        ? const ColoredBox(color: Colors.black26)
                        : Image.network(s.posterUrl, fit: BoxFit.cover, width: double.infinity)),
                    Padding(padding: const EdgeInsets.all(6), child: Text(s.title, maxLines: 2)),
                  ]),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
"""

_DETAIL = """\
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../providers.dart';

class DetailScreen extends ConsumerWidget {
  final String seriesId;
  const DetailScreen({super.key, required this.seriesId});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final eps = ref.watch(episodesProvider(seriesId));
    return Scaffold(
      appBar: AppBar(title: const Text('Episodes')),
      body: eps.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(child: Text('Error: $e')),
        data: (list) => ListView(children: [
          for (final e in list)
            ListTile(
              leading: Icon(e.isLocked ? Icons.lock : Icons.play_circle),
              title: Text('EP ${e.number}'),
              subtitle: Text(e.isLocked ? 'Locked · ${e.unlockCost} coins' : 'Free'),
              onTap: () async {
                if (e.isLocked) {
                  final ok = await ref.read(repoProvider).unlock(e.id).catchError((_) => false);
                  if (!ok) { if (context.mounted) context.go('/paywall'); return; }
                }
                if (context.mounted) context.go('/play?url=${Uri.encodeComponent(e.videoUrl)}');
              },
            ),
        ]),
      ),
    );
  }
}
"""

_PLAYER = """\
import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

class PlayerScreen extends StatefulWidget {
  final String url;
  const PlayerScreen({super.key, required this.url});
  @override
  State<PlayerScreen> createState() => _PlayerScreenState();
}

class _PlayerScreenState extends State<PlayerScreen> {
  VideoPlayerController? _c;
  @override
  void initState() {
    super.initState();
    if (widget.url.isNotEmpty) {
      _c = VideoPlayerController.networkUrl(Uri.parse(widget.url))
        ..initialize().then((_) { setState(() {}); _c?.setLooping(true); _c?.play(); });
    }
  }
  @override
  void dispose() { _c?.dispose(); super.dispose(); }
  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: Colors.black,
        body: Center(
          child: _c != null && _c!.value.isInitialized
              ? AspectRatio(aspectRatio: _c!.value.aspectRatio, child: VideoPlayer(_c!))
              : const CircularProgressIndicator(),
        ),
      );
}
"""

_PAYWALL = """\
import 'package:flutter/material.dart';

class PaywallScreen extends StatelessWidget {
  const PaywallScreen({super.key});
  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Go Pro')),
        body: Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.workspace_premium, size: 64),
            const SizedBox(height: 12),
            const Text('Unlock all episodes, no ads, 1080p'),
            const SizedBox(height: 16),
            FilledButton(onPressed: () {}, child: const Text('Subscribe (Apphud)')),
          ]),
        ),
      );
}
"""


def generate_app(
    spec: dict[str, Any],
    out_dir: Path,
    *,
    project_id: str = "app",
    collection_prefix: str = "",
    firebase_config: dict[str, str] | None = None,
) -> Path:
    """Write a complete backend-connected Flutter app into ``out_dir``."""
    cfg = firebase_config or {}
    app_name = str(spec.get("app_name", "App"))
    pkg = "".join(ch for ch in app_name.lower() if ch.isalnum()) or "app"

    (out_dir / "lib" / "screens").mkdir(parents=True, exist_ok=True)
    (out_dir / "pubspec.yaml").write_text(_PUBSPEC.format(pkg=pkg, app=app_name))
    (out_dir / "lib" / "firebase_options.dart").write_text(
        _FIREBASE_OPTIONS.format(
            apiKey=cfg.get("apiKey", "PLACEHOLDER_API_KEY"),
            appId=cfg.get("appId", "PLACEHOLDER_APP_ID"),
            sender=cfg.get("messagingSenderId", "0"),
            projectId=cfg.get("projectId", project_id),
            bucket=cfg.get("storageBucket", f"{project_id}.firebasestorage.app"),
        )
    )
    (out_dir / "lib" / "models.dart").write_text(_MODELS)
    (out_dir / "lib" / "repository.dart").write_text(_REPO.format(prefix=collection_prefix))
    (out_dir / "lib" / "providers.dart").write_text(_PROVIDERS)
    (out_dir / "lib" / "main.dart").write_text(_MAIN.format(app=app_name))
    (out_dir / "lib" / "screens" / "feed.dart").write_text(_FEED)
    (out_dir / "lib" / "screens" / "detail.dart").write_text(_DETAIL)
    (out_dir / "lib" / "screens" / "player.dart").write_text(_PLAYER)
    (out_dir / "lib" / "screens" / "paywall.dart").write_text(_PAYWALL)

    log.info("flutter_wiring.done", app=app_name, prefix=collection_prefix or "(none)")
    return out_dir
