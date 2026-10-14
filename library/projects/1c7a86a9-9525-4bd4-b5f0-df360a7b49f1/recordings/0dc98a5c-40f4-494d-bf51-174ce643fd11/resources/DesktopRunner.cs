// TestForge Windows desktop bridge client (.NET 6+).
// Reads desktop-steps.json and posts each step, in order, to the authenticated
// Windows UIA agent named by TF_WINDOWS_AGENT_URL / TF_WINDOWS_AGENT_TOKEN.
// Without an agent it exits with guidance; local simulation is desktop_test.py.
// dotnet run --project .  (place this file alone in a console project folder)
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

var url = Environment.GetEnvironmentVariable("TF_WINDOWS_AGENT_URL");
var token = Environment.GetEnvironmentVariable("TF_WINDOWS_AGENT_TOKEN");
if (string.IsNullOrWhiteSpace(url) || string.IsNullOrWhiteSpace(token))
{
    Console.Error.WriteLine("Set TF_WINDOWS_AGENT_URL and TF_WINDOWS_AGENT_TOKEN, or run desktop_test.py for the local simulator.");
    return 2;
}
var steps = JsonDocument.Parse(await File.ReadAllTextAsync("desktop-steps.json")).RootElement.GetProperty("steps");
using var http = new HttpClient { BaseAddress = new Uri(url) };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
foreach (var step in steps.EnumerateArray())
{
    var payload = JsonSerializer.Serialize(new { step });
    using var response = await http.PostAsync("/commands", new StringContent(payload, Encoding.UTF8, "application/json"));
    response.EnsureSuccessStatusCode();
    Console.WriteLine($"step ok: {step.GetProperty("action").GetString()}");
}
Console.WriteLine("desktop steps passed (Windows agent, sequential)");
return 0;
