# Copyright (C) 2013-2014  Matthieu Caneill <matthieu.caneill@gmail.com>
#                          Stefano Zacchiroli <zack@upsilon.cc>
#
# This file is part of Debsources.
#
# Debsources is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, magic, stat

from sqlalchemy import func as sql_func
from sqlalchemy import Column, ForeignKey, UniqueConstraint
from sqlalchemy import Integer, String, Index, Enum, LargeBinary
from sqlalchemy import and_
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

from debian.debian_support import version_compare

from excepts import InvalidPackageOrVersionError, FileOrFolderNotFound
from consts import MAX_KEY_LENGTH, VCS_TYPES, SLOCCOUNT_LANGUAGES, \
    CTAGS_LANGUAGES, METRIC_TYPES, AREAS, PREFIXES_DEFAULT
import filetype

Base = declarative_base()


class Package(Base):
    """ a source package """
    __tablename__ = 'packages'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True, unique=True)
    versions = relationship("Version", backref="package",
                            cascade="all, delete-orphan",
                            passive_deletes=True)
    
    def __init__(self, name):
        self.name = name
        
    def __repr__(self):
        return self.name
    
    @staticmethod
    def get_packages_prefixes(cache_dir):
        """
        returns the packages prefixes (a, b, ..., liba, libb, ..., y, z)
        cache_dir: the cache directory, usually comes from the app config
        """
        try:
            with open(os.path.join(cache_dir, 'pkg-prefixes')) as f:
                prefixes = [ l.rstrip() for l in f ]
        except IOError:
            prefixes = PREFIXES_DEFAULT
        return prefixes

    
    @staticmethod
    def list_versions_from_name(session, packagename):
         try:
             package_id = session.query(Package).filter(
                 Package.name==packagename).first().id
         except Exception as e:
             raise InvalidPackageOrVersionError(packagename)
         try:
             versions = session.query(Version).filter(
                 Version.package_id==package_id).all()
         except Exception as e:
             raise e
             raise InvalidPackageOrVersionError(packagename)
         # we sort the versions according to debian versions rules
         versions = sorted(versions, cmp=version_compare)
         return versions
    
    def to_dict(self):
        """
        simply serializes a package (because SQLAlchemy query results
        aren't serializable
        """
        return dict(name=self.name)


class Version(Base):
    """ a version of a source package """
    __tablename__ = 'versions'
    
    id = Column(Integer, primary_key=True)
    vnumber = Column(String)
    package_id = Column(Integer, ForeignKey('packages.id', ondelete="CASCADE"),
                        nullable=False)
    area = Column(String(8), index=True)	# main, contrib, non-free
    vcs_type = Column(Enum(*VCS_TYPES, name="vcs_types"))
    vcs_url = Column(String)
    vcs_browser = Column(String)
    
    def __init__(self, version, package):
        self.vnumber = version
        self.package_id = package.id

    def __repr__(self):
        return self.vnumber
    
    def to_dict(self):
        """
        simply serializes a version (because SQLAlchemy query results
        aren't serializable
        """
        return dict(vnumber=self.vnumber, area=self.area)


Index('ix_versions_package_id_vnumber', Version.package_id, Version.vnumber)


class SuitesMapping(Base):
    """
    Debian suites (squeeze, wheezy, etc) mapping with source package versions
    """
    __tablename__ = 'suitesmapping'
    
    id = Column(Integer, primary_key=True)
    sourceversion_id = Column(Integer,
                              ForeignKey('versions.id', ondelete="CASCADE"),
                              nullable=False)
    suite = Column(String, index=True)
    
    def __init__(self, version, suite):
        self.sourceversion_id = version.id
        self.suite = suite


class File(Base):
    """source file table"""

    __tablename__ = 'files'
    __table_args__ = (UniqueConstraint('version_id', 'path'),)

    id = Column(Integer, primary_key=True)
    version_id = Column(Integer, ForeignKey('versions.id', ondelete="CASCADE"),
                        nullable=False)
    path = Column(LargeBinary, index=True,	# path/whitin/source/pkg
                  nullable=False)

    def __init__(self, version, path):
        self.version_id = version.id
        self.path = path


class Checksum(Base):
    __tablename__ = 'checksums'
    __table_args__ = (UniqueConstraint('version_id', 'file_id'),)

    id = Column(Integer, primary_key=True)
    version_id = Column(Integer, ForeignKey('versions.id', ondelete="CASCADE"),
                        nullable=False)
    file_id = Column(Integer, ForeignKey('files.id', ondelete="CASCADE"),
                     nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)

    def __init__(self, version, file_, sha256):
        self.version_id = version.id
        self.file_id = file_.id
        self.sha256 = sha256
    

    @staticmethod
    def _query_checksum(session, checksum, package=None):
        """
        Returns the query used to retrieve checksums/count checksums.
        """
        query = (session.query(Package.name.label("package"),
                               Version.vnumber.label("version"),
                               Checksum.file_id.label("file_id"),
                               File.path.label("path"))
                 .filter(Checksum.sha256 == checksum)
                 .filter(Checksum.version_id == Version.id)
                 .filter(Checksum.file_id == File.id)
                 .filter(Version.package_id == Package.id)
             )
        if package is not None and package != "":
            query = query.filter(Package.name == package)
        
        query = query.order_by("package", "version", "path")
        return query
        

    @staticmethod
    def files_with_sum(session, checksum, slice_=None, package=None):
        """
        Returns a list of files whose hexdigest is checksum.
        You can slice the results, passing slice=(start, end).
        """
        # here we use db.session.query() instead of Class.query,
        # because after all "pure" SQLAlchemy is better than the
        # Flask-SQLAlchemy plugin.
        results = Checksum._query_checksum(session, checksum, package=package)
        
        if slice_ is not None:
            results = results.slice(slice_[0], slice_[1])
        results = results.all()
        
        return [dict(path=res.path,
                     package=res.package,
                     version=res.version)
                for res in results]
    
    @staticmethod
    def count_files_with_sum(session, checksum, package=None):
        count = (Checksum._query_checksum(session, checksum, package=package)
                 .count())
        
        return count



class BinaryPackage(Base):
    __tablename__ = 'binarypackages'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True, unique=True)
    versions = relationship("BinaryVersion", backref="binarypackage",
                            cascade="all, delete-orphan",
                            passive_deletes=True)
    
    def __init__(self, name):
        self.name = name
        
    def __repr__(self):
        return self.name


class BinaryVersion(Base):
    __tablename__ = 'binaryversions'
    
    id = Column(Integer, primary_key=True)
    vnumber = Column(String)
    binarypackage_id = Column(Integer, ForeignKey('binarypackages.id',
                                                  ondelete="CASCADE"),
                              nullable=False)
    sourceversion_id = Column(Integer, ForeignKey('versions.id',
                                                  ondelete="CASCADE"),
                              nullable=False)
    
    def __init__(self, vnumber, area="main"):
        self.vnumber = vnumber

    def __repr__(self):
        return self.vnumber


class SlocCount(Base):
    __tablename__ = 'sloccounts'
    __table_args__ = (UniqueConstraint('sourceversion_id', 'language'),)
    
    id = Column(Integer, primary_key=True)
    sourceversion_id = Column(Integer,
                              ForeignKey('versions.id', ondelete="CASCADE"),
                              nullable=False)
    language = Column(Enum(*SLOCCOUNT_LANGUAGES, name="language_names"),
                      # TODO rename enum s/language_names/sloccount/languages
                      nullable=False)
    count = Column(Integer, nullable=False)

    def __init__(self, version, lang, locs):
        self.sourceversion_id = version.id
        self.language = lang
        self.count = locs


class Ctag(Base):
    __tablename__ = 'ctags'

    id = Column(Integer, primary_key=True)
    version_id = Column(Integer, ForeignKey('versions.id', ondelete="CASCADE"),
                        nullable=False, index=True)
    tag = Column(String, nullable=False, index=True)
    file_id = Column(Integer, ForeignKey('files.id', ondelete="CASCADE"),
                     nullable=False)
    line = Column(Integer, nullable=False)
    kind = Column(String)	# see `ctags --list-kinds`; unfortunately ctags
        # gives no guarantee of uniformity in kinds, they might be one-lettered
        # or full names, sigh
    language = Column(Enum(*CTAGS_LANGUAGES, name="ctags_languages"))

    def __init__(self, version, tag, file_, line, kind, language):
        self.version_id = version.id
        self.tag = tag
        self.file_id = file_.id
        self.line = line
        self.kind = kind
        self.language = language
    
    # TODO:
    # after refactoring, when we'll have a File table
    # the query to get a list of files containing a list of tags will be simpler
    #
    # def find_files_containing(self, session, ctags, package=None):
    #     """
    #     Returns a list of files containing all the ctags.
        
    #     session: SQLAlchemy session
    #     ctags: [tags]
    #     package: limit search in package
    #     """
    #     results = (session.query(Ctag.path, Ctag.version_id)
    #                .filter(Ctag.tag in ctags)
    #                .filter(Ctag
    
    @staticmethod
    def find_ctag(session, ctag, package=None, slice_=None):
        """
        Returns places in the code where a ctag is found.
             tuple (count, [sliced] results)
        
        session: an SQLAlchemy session
        ctag: the ctag to search
        package: limit results to package
        """
        
        results = (session.query(Package.name.label("package"),
                                 Version.vnumber.label("version"),
                                 Ctag.file_id.label("file_id"),
                                 File.path.label("path"),
                                 Ctag.line.label("line"))
                   .filter(Ctag.tag == ctag)
                   .filter(Ctag.version_id == Version.id)
                   .filter(Ctag.file_id == File.id)
                   .filter(Version.package_id == Package.id)
                   )
        if package is not None:
            results = results.filter(Package.name == package)
        
        results = results.order_by(Ctag.version_id, File.path)
        count = results.count()
        if slice_ is not None:
            results = results.slice(slice_[0], slice_[1])
        results = [dict(package=res.package,
                        version=res.version,
                        path=res.path,
                        line=res.line)
                   for res in results.all()]
        return (count, results)



class Metric(Base):
    __tablename__ = 'metrics'
    __table_args__ = (UniqueConstraint('sourceversion_id', 'metric'),)

    id = Column(Integer, primary_key=True)
    sourceversion_id = Column(Integer,
                              ForeignKey('versions.id', ondelete="CASCADE"),
                              nullable=False)
    metric = Column(Enum(*METRIC_TYPES, name="metric_types"), nullable=False)
    value = Column("value_", Integer, nullable=False)

    def __init__(self, version, metric, value):
        self.sourceversion_id = version.id
        self.metric = metric
        self.value = value


class Location(object):
    """ a location in a package, can be a directory or a file """
    
    def _get_debian_path(self, session, package, version, sources_dir):
        """
        Returns the Debian path of a package version.
        For example: main/h
                     contrib/libz
        It's the path of a *version*, since a package can have multiple
        versions in multiple areas (ie main/contrib/nonfree).
        
        sources_dir: the sources directory, usually comes from the app config
        """
        if package[0:3] == "lib":
            prefix = package[0:4]
        else:
            prefix = package[0]
        
        try:
            p_id = session.query(Package).filter(
                Package.name==package).first().id
            varea = session.query(Version).filter(and_(
                        Version.package_id==p_id,
                        Version.vnumber==version)).first().area
        except:
            # the package or version doesn't exist in the database
            # BUT: packages are stored for a longer time in the filesystem
            # to allow codesearch.d.n and others less up-to-date platforms
            # to point here.
            # Problem: we don't know the area of such a package
            # so we try in main, contrib and non-free.
            for area in AREAS:
                if os.path.exists(os.path.join(sources_dir,
                                          area, prefix, package, version)):
                    return os.path.join(area, prefix)
            
            raise InvalidPackageOrVersionError("%s %s" % (package, version))
        
        return os.path.join(varea, prefix)
    
    def __init__(self, session, sources_dir, sources_static,
                 package, version="", path=""):
        """ initialises useful attributes """
        debian_path = self._get_debian_path(session,
                                            package, version, sources_dir)
        self.package = package
        self.version = version
        self.path = path
        self.path_to = os.path.join(package, version, path)
        
        self.sources_path = os.path.join(
            sources_dir,
            debian_path,
            self.path_to)

        if not(os.path.exists(self.sources_path)):
            raise FileOrFolderNotFound("%s" % (self.path_to))
        
        self.sources_path_static = os.path.join(
            sources_static,
            debian_path,
            self.path_to)
    
    def is_dir(self):
        """ True if self is a directory, False if it's not """
        return os.path.isdir(self.sources_path)
    
    def is_file(self):
        """ True if sels is a file, False if it's not """
        return os.path.isfile(self.sources_path)

    def issymlink(self):
        """
        True if a folder/file is a symbolic link file, False if it's not
        """
        return os.path.islink(self.sources_path)
    
    def get_package(self):
        return self.package
    
    def get_version(self):
        return self.version
    
    def get_path(self):
        return self.path
    
    def get_deepest_element(self):
        if self.version == "":
            return self.package
        elif self.path == "":
            return self.version
        else:
            return self.path.split("/")[-1]
        
    def get_path_to(self):
        return self.path_to.rstrip("/")
    
    @staticmethod
    def get_path_links(endpoint, path_to):
        """
        returns the path hierarchy with urls, to use with 'You are here:'
        [(name, url(name)), (...), ...]
        """
        path_dict = path_to.split('/')
        pathl = []
        
        # we import flask here, in order to permit the use of this module
        # without requiring the user to have flask (e.g. bin/update-debsources
        # can run in another machine without flask, because it doesn't use
        # this method)
        from flask import url_for
        
        for (i, p) in enumerate(path_dict):
            pathl.append((p, url_for(endpoint,
                                     path_to='/'.join(path_dict[:i+1]))))
        return pathl



class Directory(object):
    """ a folder in a package """
    
    def __init__(self, location, toplevel=False):
        # if the directory is a toplevel one, we remove the .pc folder
        self.sources_path = location.sources_path
        self.toplevel = toplevel

    def get_listing(self):
        """
        returns the list of folders/files in a directory,
        along with their type (directory/file)
        in a tuple (name, type)
        """
        def get_type(f):
            if os.path.isdir(os.path.join(self.sources_path, f)):
                return "directory"
            else: 
                return "file"
        listing = sorted(dict(name=f, type=get_type(f))
                         for f in os.listdir(self.sources_path))
        if self.toplevel:
            listing = filter(lambda x: x['name'] != ".pc", listing)
        
        return listing
    


class SourceFile(object):
    """ a source file in a package """

    def __init__(self, location):
        self.location = location
        self.sources_path = location.sources_path
        self.sources_path_static = location.sources_path_static
        self.mime = self._find_mime()
    
    def _find_mime(self):
        """ returns the mime encoding and type of a file """
        mime = magic.open(magic.MIME_TYPE)
        mime.load()
        type_ = mime.file(self.sources_path)
        mime.close()
        mime = magic.open(magic.MIME_ENCODING)
        mime.load()
        encoding = mime.file(self.sources_path)
        mime.close()
        return dict(encoding=encoding, type=type_)
    
    def get_mime(self):
        return self.mime
    
    def get_sha256sum(self, session):
        """
        Queries the DB and returns the shasum of the file.
        """
        try:
            shasum = (session.query(Checksum.sha256)
                      .filter(Checksum.version_id==Version.id)
                      .filter(Version.package_id==Package.id)
                      .filter(Package.name==self.location.package)
                      .filter(Version.vnumber==self.location.version)
                      # WARNING:
                      # in the DB path is binary,
                      # and here location.path is unicode, because the path
                      # comes from the URL.
                      # TODO: check with non-unicode paths
                      .filter(Checksum.path==str(self.location.path))
                      .first()
                      )[0]
        except Exception as e:
            #app.logger.error(e)
            shasum = None
        return shasum
    
    def get_permissions(self):
        """
        Returns the permissions of the folder/file on the disk, unix-styled.
        """
        read = ("-", "r")
        write = ("-", "w")
        execute = ("-", "x")
        flags = [
            (stat.S_IRUSR, "r", "-"),
            (stat.S_IWUSR, "w", "-"),
            (stat.S_IXUSR, "x", "-"),
            (stat.S_IRGRP, "r", "-"),
            (stat.S_IWGRP, "w", "-"),
            (stat.S_IXGRP, "x", "-"),
            (stat.S_IROTH, "r", "-"),
            (stat.S_IWOTH, "w", "-"),
            (stat.S_IXOTH, "x", "-"),
            ]
        perms = os.stat(self.sources_path).st_mode
        unix_style = ""
        for (flag, do_true, do_false) in flags:
            unix_style += do_true if (perms & flag) else do_false
        
        return unix_style


    def istextfile(self):
        """ 
        True if self is a text file, False if it's not.
        """
        return filetype.is_text_file(self.mime['type'])
        # for substring in text_file_mimes:
        #     if substring in self.mime['type']:
        #         return True
        # return False
        
    def get_raw_url(self):
        """ return the raw url on disk (e.g. data/main/a/azerty/foo.bar) """
        return self.sources_path_static

