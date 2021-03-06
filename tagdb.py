"""This module provides a class that lets one associate tags and values
with a set of ids, and then select ids based on those tags and values.
For example query("deep56,night,ar2,in(bounds,Moon)") woult return a
set of ids with the tags deep56, night and ar2, and where th Moon[2]
array is in the polygon specified by the bounds [:,2] array."""
import re, numpy as np, h5py, shlex, copy, warnings, time
from enlib import utils

class Tagdb:
	def __init__(self, data=None, sort="id", default_fields=[], default_query=""):
		"""Most basic constructor. Takes a dictionary
		data[name][...,nid], which must contain the field "id"."""
		if data is None:
			self.data = {"id":np.zeros(0,dtype='S5'),"subids":np.zeros(0,dtype='S5')}
		else:
			self.data = {key:np.array(val) for key,val in data.iteritems()}
			assert "id" in self.data, "Id field missing"
			if self.data["id"].size == 0: self.data["id"] = np.zeros(0,dtype='S5')
		# Insert subids if missing
		if "subids" not in self.data:
			self.data["subids"] = np.zeros(len(self.data["id"]),dtype='S5')
		# Inser default fields. These will always be present, but will be
		for field_expr in default_fields:
			if isinstance(field_expr, basestring): field_expr = (field_expr,)
			name  = field_expr[0]
			value = field_expr[1] if len(field_expr) > 1 else False
			dtype = field_expr[2] if len(field_expr) > 2 else type(value)
			if name not in self.data:
				self.data[name] = np.full(len(self), value, dtype=dtype)
		# Register the default query. This will be suffixed to each query.
		self.default_query = default_query
		# Set our sorting field
		self.sort  = sort
	def get_funcs(self):
		return {"file_contains": file_contains}
	def copy(self):
		return copy.deepcopy(self)
	@property
	def ids(self):
		return append_subs(self.data["id"], self.data["subids"])
	def __len__(self): return len(self.ids)
	def __getitem__(self, query=""):
		return self.query(query)
	def select(self, ids):
		"""Return a tagdb which only contains the selected ids."""
		# Extract the subids
		ids, subids = split_ids(ids)
		# Restrict to the subset of these ids
		inds = utils.find(self.ids, ids)
		odata = {key:val[...,inds] for key, val in self.data.iteritems()}
		# Update subids
		odata["subids"] = np.array([merge_subid(a,b) for a, b in zip(odata["subids"], subids)])
		res = self.copy()
		res.data = odata
		return res
	def query(self, query=None, apply_default_query=True):
		"""Query the database. The query takes the form
		tag,tag,tag,...:sort[slice], where all tags must be satisfied for an id to
		be returned. More general syntax is also available. For example,
		(a+b>c)|foo&bar,cow. This follows standard python and numpy syntax,
		except that , is treated as a lower-priority version of &."""
		# First split off any sorting field or slice
		if query is None: query = ""
		toks = utils.split_outside(query,":")
		query, rest = toks[0], ":".join(toks[1:])
		# Hack: Support id fields as tags, even if they contain
		# illegal characters..
		t1 = time.time()
		for id in self.data["id"]:
			if id not in query: continue
			query = re.sub(r"""(?<!['"])\b%s\b""" % id, "(id=='%s')" % id, query)
		# Split into ,-separated fields. Fields starting with a "+"
		# are taken to be tag markers, and are simply propagated to the
		# resulting ids.
		toks = utils.split_outside(query,",")
		fields, subid = [], []
		override_ids = None
		for tok in toks:
			if len(tok) == 0: continue
			if tok.startswith("+"):
				# Tags starting with + will be interpreted as a subid specification
				subid.append(tok[1:])
			elif tok.startswith("/"):
				# Tags starting with / will be interpreted as special query flags
				if tok == "/all": apply_default_query = False
				else: raise ValueError("Unknown query flag '%s'" % tok)
			else:
				# Normal field. Perform a few convenience transformations first.
				if tok.startswith("@@"):
					# Hack. *Force* the given ids to be returned, even if they aren't in the database.
					override_ids = load_ids(tok[2:])
					continue
				elif tok.startswith("@"):
					# Restrict dataset to those in the given file
					tok = "file_contains('%s',id)" % tok[1:]
				fields.append(tok)
		if override_ids is not None:
			# Append subids to our ids, and return immediately. All other fields
			# and queries are ignored.
			subs = np.array(",".join(subid))
			subs = np.full(len(override_ids), subs, subs.dtype)
			return append_subs(override_ids, subs)
		# Apply our default queries here. These are things that we almost always
		# want in our queries, and that it's tedious to have to specify manually
		# each time. For example, this would be "selected" for act todinfo queries
		if apply_default_query:
			fields = fields + utils.split_outside(self.default_query,",")
		# Back to strings. For our query, we want numpy-compatible syntax,
		# with low precedence for the comma stuff.
		query = "(" + ")&(".join(fields) + ")"
		subid = ",".join(subid)
		# Evaluate the query. First build up the scope dict
		scope = np.__dict__.copy()
		scope.update(self.data)
		# Extra functions
		scope.update(self.get_funcs())
		with utils.nowarn():
			hits = eval(query, scope)
		ids  = self.data["id"][hits]
		subs = self.data["subids"][hits]
		# Split the rest into a sorting field and a slice
		toks = rest.split("[")
		if   len(toks) == 1: sort, fsel, dsel = toks[0], "", ""
		elif len(toks) == 2: sort, fsel, dsel = toks[0], "", "["+toks[1]
		else: sort, fsel, dsel = toks[0], "["+toks[1], "["+"[".join(toks[2:])
		if self.sort and not sort: sort = self.sort
		if sort:
			# Evaluate sorting field
			field = self.data[sort][hits]
			field = eval("field" + fsel)
			inds  = np.argsort(field)
			# Apply sort
			ids  = ids[inds]
			subs = subs[inds]
		# Finally apply the data slice
		ids = eval("ids"  + dsel)
		subs= eval("subs" + dsel)
		# Build our subid extensions and append them to ids
		subs = np.array([merge_subid(subid, sub) for sub in subs])
		ids = append_subs(ids, subs)
		return ids
	def __add__(self, other):
		"""Produce a new tagdb which contains the union of the
		tag info from each."""
		res = self.copy()
		res.data = merge([self.data,other.data])
		return res
	def write(self, fname, type=None):
		write(fname, self, type=type)
	@classmethod
	def read(cls, fname, type=None):
		"""Read a Tagdb from in either the hdf or text format. This is
		chosen automatically based on the file extension."""
		if type is None:
			if fname.endswith(".hdf"): type = "hdf"
			else: type = "txt"
		if type == "txt":   return cls.read_txt(fname)
		elif type == "hdf": return cls.read_hdf(fname)
		else: raise ValueError("Unknown Tagdb file type: %s" % fname)
	@classmethod
	def read_txt(cls, fname):
		"""Read a Tagdb from text files. Only supports boolean tags."""
		datas = []
		for subfile, tags in parse_tagfile_top(fname):
			ids = parse_tagfile_idlist(subfile)
			data = {"id":ids}
			for tag in tags:
				data[tag] = np.full(len(ids), True, dtype=bool)
			datas.append(data)
		return cls(merge(datas))
	@classmethod
	def read_hdf(cls, fname):
		"""Read a Tagdb from an hdf file."""
		data = {}
		with h5py.File(fname, "r") as hfile:
			for key in hfile:
				data[key] = hfile[key].value
		return cls(data)
	def write(self, fname, type=None):
		"""Write a Tagdb in either the hdf or text format. This is
		chosen automatically based on the file extension."""
		if type is None:
			if fname.endswith(".hdf"): type = "hdf"
			else: type = "txt"
		if type == "txt":   raise NotImplementedError
		elif type == "hdf": return self.write_hdf(fname)
		else: raise ValueError("Unknown Tagdb file type: %s" % fname)
	def write_hdf(self, fname):
		"""Write a Tagdb to an hdf file."""
		with h5py.File(fname, "w") as hfile:
			for key in self.data:
				hfile[key] = self.data[key]

# We want a way to build a dtype from file. Two main ways will be handy:
# 1: The tag fileset.
#    Consists of a main file with lines like
#    filename tag tag tag ...
#    where each named file contains one id per line
#    (though in practice there may be other stuff on the lines that needs cleaning...)
# 2: An hdf file

def read(fname, type=None): return Tagdb.read(fname, type=type)
def read_txt(fname): return Tagdb.read_txt(fname)
def read_hdf(fname): return Tagdb.read_hdf(fname)

def write(fname, tagdb, type=None): return tagdb.write(fname, type=type)
def write_hdf(fname, tagdb): return tagdb.write(fname)

def merge(tagdatas):
	"""Merge two or more tagdbs into a total one, which will have the
	union of the ids."""
	# First get rid of empty inputs
	tagdatas = [data for data in tagdatas if len(data["id"]) > 0]
	# Generate the union of ids, and the index of each
	# tagset into it.
	tot_ids = utils.union([data["id"] for data in tagdatas])
	inds = [utils.find(tot_ids, data["id"]) for data in tagdatas]
	nid  = len(tot_ids)
	data_tot = {}
	for di, data in enumerate(tagdatas):
		for key, val in data.iteritems():
			if key not in data_tot:
				# Hard to find an appropriate default value for
				# all types. We use false for bool to let tags
				# accumulate, -1 as probably the most common
				# placeholder value for ints, and NaN for strings
				# and floats.
				oval = np.zeros(val.shape[:-1]+(nid,),val.dtype)
				if oval.dtype == bool:
					oval[:] = False
				elif np.issubdtype(oval.dtype, np.integer):
					oval[:] = -1
				else:
					oval[:] = np.NaN
				if oval.dtype == bool: oval[:] = False
				data_tot[key] = oval
			# Boolean flags combine OR-wise, to let us mention the same
			# id in multiple files
			if val.dtype == bool: data_tot[key][...,inds[di]] |= val
			else: data_tot[key][...,inds[di]] = val
	return data_tot

def parse_tagfile_top(fname):
	"""Read and parse the top-level tagfile in the Tagdb text format.
	Contains lines of the form [filename tag tag tag ...]. Also supports
	comments (#) and variables (foo = bar), which can be referred to later
	as {foo}. Returns a list of (fname, tagset) tuples."""
	res  = []
	vars = {}
	with open(fname,"r") as f:
		for line in f:
			line = line.rstrip()
			if not line or len(line) < 1 or line[0] == "#": continue
			toks = shlex.split(line)
			assert len(toks) > 1, "Tagdb entry needs at least one tag: '%s'" % line
			if toks[1] == "=":
				vars[toks[0]] = toks[2]
			else:
				res.append((toks[0].format(**vars), set(toks[1:])))
	return res

def parse_tagfile_idlist(fname):
	"""Reads a file containing an id per line, and returns the ids as a list."""
	res = []
	with open(fname,"r") as f:
		for line in f:
			line = line.rstrip()
			if len(line) < 1 or line[0] == "#": continue
			res.append(line.split()[0])
	return res

def file_contains(fname, ids):
	lines = [line.split()[0] for line in open(fname,"r") if not line.startswith("#")]
	return utils.contains(ids, lines)

def load_ids(fname):
	lines = [line.split()[0] for line in open(fname,"r") if not line.startswith("#")]
	return np.array(lines)

def split_ids(ids):
	bids, subids = [], []
	for id in ids:
		toks = id.split(":")
		bids.append(toks[0])
		subids.append(toks[1] if len(toks) > 1 else "")
	return bids, subids

def merge_subid(a, b):
	res = set(a.split(",")) | set(b.split(","))
	try: res.remove("")
	except: pass
	return ",".join(sorted(list(res)))

def append_subs(ids, subs):
	if len(ids) == 0: return ids
	sep_helper = np.array(["",":"])
	ind = (np.char.str_len(subs) > 0).astype(int)
	sep = sep_helper[ind]
	return np.char.add(ids, np.char.add(sep, subs))
