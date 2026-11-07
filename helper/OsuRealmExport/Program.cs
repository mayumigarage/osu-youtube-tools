// OsuRealmExport: reads a COPY of osu!lazer's client.realm in dynamic, read-only mode
// and writes the beatmap set list as JSON.
//
//   OsuRealmExport export --realm <copied client.realm> --out <out.json>
//   OsuRealmExport schema --realm <copied client.realm>
//
// Exit codes: 0 ok, 1 unexpected error, 2 realm could not be opened, 3 required class missing, 64 usage.

using System.Text.Encodings.Web;
using System.Text.Json;
using Realms;
using Realms.Schema;

const int ExitOk = 0, ExitError = 1, ExitOpen = 2, ExitSchema = 3;

if (args.Length < 1 || (args[0] != "export" && args[0] != "schema"))
    return Usage();

var command = args[0];
string? realmPath = null, outPath = null;
for (int i = 1; i < args.Length; i++)
{
    switch (args[i])
    {
        case "--realm" when i + 1 < args.Length: realmPath = args[++i]; break;
        case "--out" when i + 1 < args.Length: outPath = args[++i]; break;
        default: return Usage();
    }
}
if (realmPath == null || (command == "export" && outPath == null))
    return Usage();

Realm realm;
try
{
    var config = new RealmConfiguration(Path.GetFullPath(realmPath)) { IsDynamic = true, IsReadOnly = true };
    realm = Realm.GetInstance(config);
}
catch (Exception e)
{
    Console.Error.WriteLine($"Could not open realm: {e.GetType().Name}: {e.Message}");
    return ExitOpen;
}

try
{
    using (realm)
    {
        if (command == "schema")
        {
            Console.OutputEncoding = System.Text.Encoding.UTF8;
            foreach (var os in realm.Schema.OrderBy(o => o.Name))
            {
                Console.WriteLine($"{os.Name} ({os.BaseType})");
                foreach (var p in os)
                    Console.WriteLine($"  {p.Name}: {p.Type}{(p.ObjectType != null ? " -> " + p.ObjectType : "")}");
            }
            return ExitOk;
        }

        if (!realm.Schema.TryFindObjectSchema("BeatmapSet", out _))
        {
            Console.Error.WriteLine("Class 'BeatmapSet' not found in realm schema.");
            return ExitSchema;
        }

        var reader = new Reader();
        var sets = realm.DynamicApi.All("BeatmapSet").ToList().Select(reader.ReadSet).ToList();
        var doc = new Dictionary<string, object?>
        {
            ["format"] = 1,
            ["exportedAt"] = DateTimeOffset.UtcNow,
            ["warnings"] = reader.Warnings.OrderBy(w => w).ToList(),
            ["sets"] = sets,
        };

        var options = new JsonSerializerOptions
        {
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            WriteIndented = false,
        };
        var full = Path.GetFullPath(outPath!);
        var tmp = full + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(doc, options), new System.Text.UTF8Encoding(false));
        File.Move(tmp, full, overwrite: true);
        Console.WriteLine($"exported {sets.Count} sets");
        return ExitOk;
    }
}
catch (Exception e)
{
    Console.Error.WriteLine($"{e.GetType().Name}: {e.Message}");
    return ExitError;
}

static int Usage()
{
    Console.Error.WriteLine("usage: OsuRealmExport export --realm <path> --out <json>\n       OsuRealmExport schema --realm <path>");
    return 64;
}

/// <summary>Reads properties defensively: a missing property yields null and a warning instead of a crash.</summary>
sealed class Reader
{
    public readonly HashSet<string> Warnings = new();
    private readonly Dictionary<string, HashSet<string>> props = new();

    private bool Has(IRealmObjectBase o, string prop)
    {
        var schema = o.ObjectSchema!;
        if (!props.TryGetValue(schema.Name, out var set))
            props[schema.Name] = set = schema.Select(p => p.Name).ToHashSet();
        if (set.Contains(prop)) return true;
        Warnings.Add($"{schema.Name}.{prop} not found");
        return false;
    }

    private RealmValue Val(IRealmObjectBase o, string prop) =>
        Has(o, prop) ? o.DynamicApi.Get<RealmValue>(prop) : RealmValue.Null;

    public string? Str(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type == RealmValueType.String ? v.AsString() : null;
    }

    public long? Int(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type == RealmValueType.Int ? v.AsInt64() : null;
    }

    public double? Dbl(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type switch
        {
            RealmValueType.Double => v.AsDouble(),
            RealmValueType.Float => v.AsFloat(),
            RealmValueType.Int => v.AsInt64(),
            _ => null,
        };
    }

    public bool? Bool(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type == RealmValueType.Bool ? v.AsBool() : null;
    }

    public DateTimeOffset? Date(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type == RealmValueType.Date ? v.AsDate() : null;
    }

    public string? GuidStr(IRealmObjectBase o, string prop)
    {
        var v = Val(o, prop);
        return v.Type == RealmValueType.Guid ? v.AsGuid().ToString() : null;
    }

    public IRealmObjectBase? Obj(IRealmObjectBase o, string prop) =>
        Has(o, prop) ? o.DynamicApi.Get<IRealmObjectBase?>(prop) : null;

    public IList<IRealmObjectBase> List(IRealmObjectBase o, string prop) =>
        Has(o, prop) ? o.DynamicApi.GetList<IRealmObjectBase>(prop) : Array.Empty<IRealmObjectBase>();

    public Dictionary<string, object?> ReadSet(IRealmObjectBase set)
    {
        // filename (case-insensitive) -> file hash
        var files = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var usage in List(set, "Files"))
        {
            var name = Str(usage, "Filename");
            var file = Obj(usage, "File");
            var hash = file != null ? Str(file, "Hash") : null;
            if (name != null && hash != null) files.TryAdd(name, hash);
        }

        var variants = new Dictionary<(string?, string?, string?), Dictionary<string, object?>>();
        var beatmaps = List(set, "Beatmaps");
        double maxLength = 0;
        foreach (var bm in beatmaps)
        {
            var length = Dbl(bm, "Length") ?? 0;
            maxLength = Math.Max(maxLength, length);
            var meta = Obj(bm, "Metadata");
            if (meta == null) continue;

            var key = (Str(meta, "Artist"), Str(meta, "Title"), Str(meta, "AudioFile"));
            if (!variants.TryGetValue(key, out var v))
            {
                var audioFile = key.Item3;
                variants[key] = v = new Dictionary<string, object?>
                {
                    ["title"] = key.Item2,
                    ["titleUnicode"] = Str(meta, "TitleUnicode"),
                    ["artist"] = key.Item1,
                    ["artistUnicode"] = Str(meta, "ArtistUnicode"),
                    ["source"] = Str(meta, "Source"),
                    ["tags"] = Str(meta, "Tags"),
                    ["audioFile"] = audioFile,
                    ["audioHash"] = audioFile != null && files.TryGetValue(audioFile, out var h) ? h : null,
                    ["maxLengthMs"] = 0.0,
                    ["difficultyCount"] = 0,
                };
            }
            v["maxLengthMs"] = Math.Max((double)v["maxLengthMs"]!, length);
            v["difficultyCount"] = (int)v["difficultyCount"]! + 1;
        }

        return new Dictionary<string, object?>
        {
            ["id"] = GuidStr(set, "ID"),
            ["onlineId"] = Int(set, "OnlineID"),
            ["hash"] = Str(set, "Hash"),
            ["dateAdded"] = Date(set, "DateAdded"),
            ["deletePending"] = Bool(set, "DeletePending") ?? false,
            ["protected"] = Bool(set, "Protected") ?? false,
            ["difficultyCount"] = beatmaps.Count,
            ["maxLengthMs"] = maxLength,
            ["variants"] = variants.Values.ToList(),
        };
    }
}
