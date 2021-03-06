import unittest
import os
from Pyro4 import expose as pyro_expose
from plugins.singleton_host import SingletonHost
from tempfile import TemporaryDirectory
from plugins.base import ProcessPack, Process, PluginProcessBase, AVAILABLE_PROCESSES
from plugins.processes_host import ProcessesHost, active_process, find_plugin
from plugins.decorators import make_plugin, get_all_plugins
from plugins.lookup_table import PluginLookupTable


class TestSingletonHosts(unittest.TestCase):
    class SingletonProcessChecker:
        @pyro_expose
        def extract_pid(self):
            return os.getpid(), os.getppid()

    class MathTest:
        @staticmethod
        def static_do_math(a, b):
            return a * b + (b - a) * (a - b)

        @pyro_expose
        def do_math(self, a, b):
            return TestSingletonHosts.MathTest.static_do_math(a, b)

    def test_singleton_host(self):
        with TemporaryDirectory() as temp_dir:
            socket = os.path.join(temp_dir, 'test_singleton_host.sock')
            with SingletonHost(socket, 'test_singleton_host') as host:
                instance = host(TestSingletonHosts.SingletonProcessChecker)
                self.assertIsNotNone(instance)
                child_pid, parent_pid = instance.extract_pid()
                self.assertIsNotNone(child_pid)
                self.assertIsNotNone(parent_pid)
                current_pid = os.getpid()
                self.assertEqual(parent_pid, current_pid)
                self.assertNotEqual(child_pid, current_pid)

    def test_math_singleton_hosted(self):
        with TemporaryDirectory() as temp_dir:
            socket = os.path.join(temp_dir, 'test_math_singleton_hosted.sock')
            with SingletonHost(socket, 'test_math_singleton_hosted') as host:
                instance = host(TestSingletonHosts.MathTest)
                self.assertIsNotNone(instance)
                args = (1, 33)
                self.assertEqual(instance.do_math(*args), TestSingletonHosts.MathTest.static_do_math(*args))

    class ExposeLocalSingletons:
        @pyro_expose
        def get_id(self):
            return id(self)

        @pyro_expose
        def get_local_singletons_by_id(self):
            return list(SingletonHost.local_singletons_by_id().keys())

        @pyro_expose
        def get_local_singletons_by_name(self):
            return list(SingletonHost.local_singletons_by_name().keys())

    def test_local_singletons(self):
        with TemporaryDirectory() as temp_dir:
            socket = os.path.join(temp_dir, 'test_local_singletons.sock')
            with SingletonHost(socket, 'test_local_singletons') as host:
                instance = host(TestSingletonHosts.ExposeLocalSingletons)
                ids = instance.get_local_singletons_by_id()
                names = instance.get_local_singletons_by_name()
                instance_id = instance.get_id()
                self.assertIn(instance_id, ids)
                self.assertIn(TestSingletonHosts.ExposeLocalSingletons.__name__, names)
                self.assertEqual(len(ids), 1)
                self.assertEqual(len(names), 1)


class TestProcessPack(unittest.TestCase):
    def test_querying_with_process(self):
        pack = ProcessPack(*[process.value for process in Process])
        for process in Process:
            self.assertEqual(pack[process], process.value)
            self.assertEqual(pack[process.value], process.value)
            self.assertEqual(getattr(pack, process.value), process.value)
            setattr(pack, process.value, None)
            pack[process] = None  # also None, to test assignment
            self.assertIsNone(getattr(pack, process.value))
        with self.assertRaises(KeyError):
            _ = pack[dict()]
        with self.assertRaises(KeyError):
            pack[dict()] = None


class TestPluginProcess(unittest.TestCase):
    class TestProcess(PluginProcessBase):
        PLUGIN_NAME = 'main'

        @classmethod
        def process(cls):
            return active_process()

        @classmethod
        def plugin_name(cls):
            return cls.PLUGIN_NAME

        @pyro_expose
        def get_remote_pid(self):
            return os.getpid()

        @pyro_expose
        def get_sibling_pid_set(self):
            return set([instance.get_remote_pid() for instance in find_plugin(self).values() if instance is not None])

    def test_process_host(self):
        plugins = {
            TestPluginProcess.TestProcess.PLUGIN_NAME: ProcessPack(TestPluginProcess.TestProcess,
                                                                   TestPluginProcess.TestProcess,
                                                                   TestPluginProcess.TestProcess)
        }
        with ProcessesHost(plugins) as processes:
            self.assertIn('main', processes.plugin_instances)
            instance_pack = processes.plugin_instances['main']
            for process, instance in instance_pack.items():
                self.assertIsNotNone(instance)
                if instance is not None:
                    process_in_instance = instance.get_remote_process()
                    pid_in_instance = instance.get_remote_pid()
                    self.assertIsNotNone(process_in_instance)
                    self.assertIsNotNone(pid_in_instance)
                    self.assertNotEqual(pid_in_instance, os.getpid())
                    # Need to explicitly convert because the serialization engine may not preserve the Enum
                    self.assertEqual(Process(process_in_instance), process)

    def test_none_process_host(self):
        plugins = {
            'main': ProcessPack(None, None, None)
        }
        with ProcessesHost(plugins) as processes:
            instance_pack = processes.plugin_instances['main']
            for instance in instance_pack.values():
                self.assertIsNone(instance)

    def test_intra_instance_talk(self):
        plugins = {
            'main': ProcessPack(TestPluginProcess.TestProcess,
                                TestPluginProcess.TestProcess,
                                TestPluginProcess.TestProcess)
        }
        with ProcessesHost(plugins) as processes:
            instance_pack = processes.plugin_instances['main']
            pid_sets = list([instance.get_sibling_pid_set() for instance in instance_pack])
            self.assertEqual(len(pid_sets), len(AVAILABLE_PROCESSES))
            if len(pid_sets) > 0:
                for pid_set in pid_sets[1:]:
                    self.assertEqual(pid_set, pid_sets[0])


class TestPluginDecorator(unittest.TestCase):
    @make_plugin('TestPluginDecorator', Process.MAIN)
    class DecoratedProcess(PluginProcessBase):
        @pyro_expose
        def get_two(self):
            return 2

    def test_process_host(self):
        with ProcessesHost({k: v for k, v in get_all_plugins().items() if k == 'TestPluginDecorator'}) as processes:
            self.assertIn('TestPluginDecorator', processes.plugin_instances)
            instance_pack = processes.plugin_instances['TestPluginDecorator']
            for process, instance in instance_pack.items():
                if process is Process.MAIN:
                    self.assertIsNotNone(instance)
                    self.assertEqual(instance.get_two(), 2)
                else:
                    self.assertIsNone(instance)

    def test_wrong_base(self):
        with self.assertRaises(ValueError):
            @make_plugin('A', Process.MAIN)
            class NotAPlugin:
                pass
            # Dummy usage
            NotAPlugin()  # pragma: no cover

    def test_process(self):
        with self.assertRaises(ValueError):
            @make_plugin('A')
            class NotAPlugin(PluginProcessBase):
                pass
            # Dummy usage
            NotAPlugin()  # pragma: no cover
        with self.assertRaises(ValueError):
            @make_plugin('A', Process.MAIN)
            class NotAPlugin(PluginProcessBase):
                @classmethod
                def process(cls):
                    return Process.CAMERA
            # Dummy usage
            NotAPlugin()  # pragma: no cover

        @make_plugin('A')
        class APlugin(PluginProcessBase):
            @classmethod
            def process(cls):
                return Process.CAMERA
        # Dummy usage
        APlugin()  # pragma: no cover

    def test_plugin_name(self):
        with self.assertRaises(ValueError):
            @make_plugin(process=Process.MAIN)
            class NotAPlugin(PluginProcessBase):
                pass
            # Dummy usage
            NotAPlugin()  # pragma: no cover
        with self.assertRaises(ValueError):
            @make_plugin('A', Process.MAIN)
            class NotAPlugin(PluginProcessBase):
                @classmethod
                def plugin_name(cls):
                    return 'B'
            # Dummy usage
            NotAPlugin()  # pragma: no cover

        @make_plugin(process=Process.MAIN)
        class APlugin(PluginProcessBase):
            @classmethod
            def plugin_name(cls):
                return 'APlugin'
        # Dummy usage
        APlugin()  # pragma: no cover


class TestPluginLookup(unittest.TestCase):
    @make_plugin('TestPluginTable', Process.MAIN)
    class TestPluginTablePluginMain(PluginProcessBase):
        pass

    @make_plugin('TestPluginTable', Process.TELEGRAM)
    class TestPluginTablePluginTelegram(PluginProcessBase):
        pass

    def test_direct_lookup(self):
        name = TestPluginLookup.TestPluginTablePluginMain.plugin_name()
        plugin_main = TestPluginLookup.TestPluginTablePluginMain()
        plugin_telegram = TestPluginLookup.TestPluginTablePluginTelegram()
        pack = ProcessPack(main=plugin_main, telegram=plugin_telegram)
        table = PluginLookupTable({name: pack}, Process.MAIN)

        self.assertIs(table[name], pack)
        self.assertIs(table[TestPluginLookup.TestPluginTablePluginTelegram], pack)
        self.assertIs(table[TestPluginLookup.TestPluginTablePluginMain], pack)
        self.assertIs(table[plugin_main], pack)
        self.assertIs(table[plugin_telegram], pack)

        self.assertIs(table.telegram[name], plugin_telegram)
        self.assertIs(table.telegram[TestPluginLookup.TestPluginTablePluginTelegram], plugin_telegram)
        self.assertIs(table.telegram[TestPluginLookup.TestPluginTablePluginMain], plugin_telegram)
        self.assertIs(table.telegram[plugin_main], plugin_telegram)
        self.assertIs(table.telegram[plugin_telegram], plugin_telegram)

        self.assertIs(table.main[name], plugin_main)
        self.assertIs(table.main[TestPluginLookup.TestPluginTablePluginTelegram], plugin_main)
        self.assertIs(table.main[TestPluginLookup.TestPluginTablePluginMain], plugin_main)
        self.assertIs(table.main[plugin_main], plugin_main)
        self.assertIs(table.main[plugin_telegram], plugin_main)

        self.assertIs(table[Process.TELEGRAM][name], plugin_telegram)
        self.assertIs(table[Process.TELEGRAM][TestPluginLookup.TestPluginTablePluginTelegram], plugin_telegram)
        self.assertIs(table[Process.TELEGRAM][TestPluginLookup.TestPluginTablePluginMain], plugin_telegram)
        self.assertIs(table[Process.TELEGRAM][plugin_main], plugin_telegram)
        self.assertIs(table[Process.TELEGRAM][plugin_telegram], plugin_telegram)

        self.assertIs(table[Process.MAIN][name], plugin_main)
        self.assertIs(table[Process.MAIN][TestPluginLookup.TestPluginTablePluginTelegram], plugin_main)
        self.assertIs(table[Process.MAIN][TestPluginLookup.TestPluginTablePluginMain], plugin_main)
        self.assertIs(table[Process.MAIN][plugin_main], plugin_main)
        self.assertIs(table[Process.MAIN][plugin_telegram], plugin_main)

        self.assertIs(getattr(table, name), pack)
        self.assertIs(getattr(table.telegram, name), plugin_telegram)
        self.assertIs(table.telegram.TestPluginTable, plugin_telegram)
        self.assertIs(table.main.TestPluginTable, plugin_main)
        self.assertIs(table[Process.TELEGRAM].TestPluginTable, plugin_telegram)
        self.assertIs(table[Process.MAIN].TestPluginTable, plugin_main)

        self.assertIn(pack, table.values())
        self.assertIn((name, pack), table.items())
        self.assertIn(name, table.keys())

        self.assertIsNone(table[123, 123])
        self.assertIsNone(table[123, Process.MAIN])
        with self.assertRaises(KeyError):
            _ = table[1, 2, 3]
