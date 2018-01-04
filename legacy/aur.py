#!/usr/bin/python3

import utils
import json
import urllib.request as urlreq
import subprocess
import repo
import shutil

class AURBackend:
    @classmethod
    def add(cls, target):
        with utils.JSONStore(utils.Config.db('aur')) as store:
            targets = store.read([])
            if target not in targets:
                targets.append(target)
                store.write(targets)

    @classmethod
    def remove(cls, target):
        with utils.JSONStore(utils.Config.db('aur')) as store:
            targets = store.read([])
            try:
                targets.remove(target)
                store.write(targets)
            except ValueError:
                pass

    @classmethod
    def list(cls):
        with utils.JSONStore(utils.Config.db('aur')) as store:
            return store.read([])

    @classmethod
    def generate_plan(cls, aurrepo):
        return BuildPlan.for_packages(aurrepo, cls.list())

    @classmethod
    def execute_plan(cls, plan, aurrepo, chroot, chrootrepo):
        if plan.empty():
            return
        for pkgname in plan.keep:
            pkg = aurrepo.pkgfile_path(pkgname)
            chrootrepo.add(pkg)
        for builditem in plan.build:
            pkgbase = builditem.pkgbase
            pkgbuilddir = '{}/{}'.format(utils.Config.workspace('aur'), pkgbase)
            giturl = 'https://aur.archlinux.org/{}.git'.format(pkgbase)
            subprocess.run(['git', 'clone', giturl, pkgbuilddir])

            trials = utils.Config.trials()
            while True:
                try:
                    utils.sync()
                    chroot.build(pkgbuilddir)
                    for target in builditem.pkgnames:
                        built = repo.get_pkgfile_path(pkgbuilddir, target, None)
                        if built is None:
                            raise BuildError()
                        aurrepo.add(built)
                        chrootrepo.add(built)
                    break
                except BuildError:
                    trials -= 1
                    if trials == 0:
                        raise BuildError()
            shutil.rmtree(pkgbuilddir)

    @classmethod
    def autoremove(cls, plan, aurrepo):
        autoremove = []
        for pkgname in aurrepo.packages.keys():
            if pkgname in plan.built:
                continue
            if pkgname in plan.keep:
                continue
            autoremove.append(pkgname)
        for pkgname in autoremove:
            print('Removing \'{}\'...'.format(pkgname))
            aurrepo.remove(pkgname)


class BuildError(Exception):
    pass


class BuildItem:
    def __init__(self, pkgbase):
        self.pkgbase = pkgbase
        self.pkgnames = []

    def add(self, pkgname):
        self.pkgnames.append(pkgname)

    def __str__(self):
        if len(self.pkgnames) == 1 and self.pkgnames[0] == self.pkgbase:
            return self.pkgbase
        else:
            return '{}=>{}'.format(self.pkgbase, self.pkgnames)

    def __repr__(self):
        return str(self)


class BuildPlan:
    def __init__(self, build, keep, split_to_base):
        items = {}
        self.build = []
        for target in build:
            base = split_to_base[target] if target in split_to_base else target
            if base not in items:
                items[base] = BuildItem(base)
                self.build.append(items[base])
            items[base].add(target)
        self.keep = keep
        self.built = build

    def __str__(self):
        return 'BuildPlan build: {}, keep: {}'.format(self.build, self.keep)

    def empty(self):
        return len(self.build) == 0

    @classmethod
    def for_package(cls, localrepo, pkgname):
        return cls.for_packages(localrepo, [pkgname])

    @classmethod
    def for_packages(cls, localrepo, pkgnames):
        build = []
        keep = []
        split_to_base = {}
        for pkgname in pkgnames:
            cls.generate(localrepo, pkgname, [], [], build, keep, split_to_base)
        return cls(build, keep, split_to_base)

    @classmethod
    def generate(cls, localrepo, target, dependents, makedependents, build, keep, split_to_base):
        if target in build:
            # Already included in the build plan
            # A package is planned only after its MakeDepends and Depends are planned
            return
        if target in keep:
            # Already planned to keep the package in local repo
            # A package is planned only after its MakeDepends and Depends are planned
            return
        if utils.is_official_package(target):
            # Target is in the official ArchLinux repo
            # Official packages do not rely on AUR
            return
        try:
            aurinfo = AURInfo.from_package_name(target)
        except AURPackageNotFoundError:
            # A virtual package, maybe.
            return

        for depend in aurinfo.depends:
            if depend in dependents or depend in makedependents:
                # Do not revisit this package again
                continue
            cls.generate(localrepo, depend, dependents + [target],
                    makedependents, build, keep, split_to_base)

        for makedepend in aurinfo.makedepends:
            if makedepend in makedependents:
                raise CyclicMakeDependencyError(target, makedepend)
            cls.generate(localrepo, makedepend, dependents,
                    makedependents + [target], build, keep, split_to_base)

        if target in localrepo.packages and utils.vercmp(localrepo.packages[target],
                aurinfo.version) >= 0:
            keep.append(target)
            return
        if target != aurinfo.pkgbasename:
            split_to_base[target] = aurinfo.pkgbasename
        if target not in build:
            # else: already built while resolving makedepends
            build.append(target)


class CyclicMakeDependencyError(Exception):
    def __init__(self, pkgname1, pkgname2):
        super().__init__('Cyclic make dependency between \'{}\' and \'{}\''.format(pkgname1, pkgname2))


class AURInfo:
    cache_by_pkgname = {}
    AUR_RPC_URL = 'https://aur.archlinux.org/rpc/?v=5&type=info&arg[]={}'

    def __init__(self, name, pkgbasename, version, depends, makedepends):
        self.name = name
        self.pkgbasename = pkgbasename
        self.version = version
        self.depends = depends
        self.makedepends = makedepends

    @classmethod
    def from_package_name(cls, pkgname):
        if pkgname in cls.cache_by_pkgname:
            return cls.cache_by_pkgname[pkgname]
        else:
            info = cls.from_package_name_uncached(pkgname)
            cls.cache_by_pkgname[pkgname] = info
            return info

    @classmethod
    def from_package_name_uncached(cls, pkgname):
        with urlreq.urlopen(cls.AUR_RPC_URL.format(pkgname)) as response:
            decoded = json.loads(response.read().decode())
            if decoded['resultcount'] == 0:
                raise AURPackageNotFoundError(pkgname)
            result = decoded['results'][0]
            return cls(result['Name'], result['PackageBase'], result['Version'],
                    cls.pkgnames(result.get('Depends', [])),
                    cls.pkgnames(result.get('MakeDepends', [])))

    @classmethod
    def pkgname(cls, string):
        x = string
        x = x.split('>')[0]
        x = x.split('<')[0]
        x = x.split('=')[0]
        return x

    @classmethod
    def pkgnames(cls, strings):
        return [cls.pkgname(string) for string in strings]

    def __str__(self):
        return 'Package \'{}\' {} (base: \'{}\')'.format(self.name, self.version,
                self.pkgbasename)


class AURPackageNotFoundError(Exception):
    def __init__(self, name):
        super().__init__('Package \'{}\' not found on AUR'.format(name))