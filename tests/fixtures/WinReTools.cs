using System;
using System.IO;
using System.Reflection;

class WinReTools
{
    static int Main(string[] args)
    {
        string tool = Path.GetFileNameWithoutExtension(Assembly.GetEntryAssembly().Location);
        string trace = Environment.GetEnvironmentVariable("WOU_TEST_TRACE");
        string state = trace + ".value";
        File.AppendAllText(trace, tool + " " + string.Join(" ", args) + Environment.NewLine);

        if (tool == "bcdedit")
        {
            Console.WriteLine("identifier              {22222222-2222-2222-2222-222222222222}");
            Console.WriteLine("osdevice                partition=" +
                (Environment.GetEnvironmentVariable("WOU_TEST_BAD_EFI") == "1" ? "R:" : "W:"));
            return 0;
        }
        if (tool == "dism")
        {
            if (Array.IndexOf(args, "/Get-PackageInfo") >= 0)
            {
                foreach (string argument in args)
                    if (argument.StartsWith("/PackageName:"))
                        Console.WriteLine("Package Identity : " + argument.Substring(13));
                Console.WriteLine("State : " +
                    (Environment.GetEnvironmentVariable("WOU_TEST_PACKAGE_STATE") ?? "Installed"));
                return 0;
            }
            if (Array.IndexOf(args, "/Get-Packages") >= 0)
            {
                Console.WriteLine("Package Identity | State");
                if (Environment.GetEnvironmentVariable("WOU_TEST_PENDING") == "1")
                    Console.WriteLine("Package_for_Test | Install Pending");
                return 0;
            }
            string changed = Environment.GetEnvironmentVariable("WOU_TEST_DISM_HOOK_VALUE");
            if (changed != null) File.WriteAllText(state, changed);
            return int.Parse(Environment.GetEnvironmentVariable("WOU_TEST_DISM_STATUS") ?? "3010");
        }
        if (args[0] == "load" || args[0] == "unload") return 0;
        if (args[0] == "save")
        {
            if (Environment.GetEnvironmentVariable("WOU_TEST_SAVE_ERROR") == "1") return 1;
            File.WriteAllText(args[2], "stub backup");
            return 0;
        }
        if (args[0] == "add")
        {
            string value = args[Array.IndexOf(args, "/d") + 1];
            if (value == "0x0" && Environment.GetEnvironmentVariable("WOU_TEST_POST_ERROR") == "1")
                return 1;
            File.WriteAllText(state, value);
            if (value == "0x2" && Environment.GetEnvironmentVariable("WOU_TEST_PRE_ERROR") == "1")
                return 1;
            return 0;
        }
        switch (args[args.Length - 1])
        {
            case "MachineGuid":
                Console.WriteLine("    MachineGuid    REG_SZ    33333333-3333-3333-3333-333333333333");
                break;
            case "CurrentBuildNumber":
                Console.WriteLine("    CurrentBuildNumber    REG_SZ    " +
                    (Environment.GetEnvironmentVariable("WOU_TEST_BUILD") ?? "26200"));
                break;
            case "UBR":
                Console.WriteLine("    UBR    REG_DWORD    " +
                    (Environment.GetEnvironmentVariable("WOU_TEST_REVISION") ?? "0x24f1"));
                break;
            case "Default":
                Console.WriteLine("    Default    REG_DWORD    0x1");
                break;
            case "Start":
                if (Environment.GetEnvironmentVariable("WOU_TEST_HOOK_ERROR") == "1") return 1;
                Console.WriteLine("    Start    REG_DWORD    " + (File.Exists(state) ? File.ReadAllText(state) : "0x3"));
                break;
        }
        return 0;
    }
}
