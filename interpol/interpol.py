import numpy as np, time
from enlib import utils
import fortran_32, fortran_64

def build(func, interpolator, box, errlim, maxsize=None, maxtime=None, return_obox=False, *args, **kwargs):
	"""Given a function func([nin,...]) => [nout,...] and
	an interpolator class interpolator(box,[nout,...]),
	(where the input array is regularly spaced in each direction),
	which provides __call__([nin,...]) => [nout,...],
	automatically polls func and constructs an interpolator
	object that has the required accuracy inside the provided
	bounding box."""
	box     = np.asfarray(box)
	errlim  = np.asfarray(errlim)
	idim    = box.shape[1]
	n       = np.array([4]*idim) # starting mesh size
	x       = utils.grid(box, n)
	obox    = [np.inf,-np.inf]

	t0      = time.time()

	# Set up initial interpolation
	ip = interpolator(box, func(x), *args, **kwargs)

	errs = [np.inf]*idim # Max error for each *input* dimension in last refinement step
	depth = 0
	# Refine until good enough
	while True:
		depth += 1
		if maxsize and np.product(n) > maxsize:
			raise OverflowError("Maximum refinement mesh size exceeded")
		nok = 0
		# Consider accuracy for each input parameter by tentatively doubling
		# resolution for that parameter and checking how well the interpolator
		# predicts the true values.
		for i in range(idim):
			if any(errs[i] > errlim):
				if maxtime and time.time() - t0 > maxtime:
					raise OverflowError("Maximum refinement time exceeded")
				# Grid may not be good enough in this direction.
				# Try doubling resolution
				nnew   = n.copy()
				nnew[i]= nnew[i]*2+1
				x      = utils.grid(box, nnew)
				yinter = ip(x)
				ytrue  = func(x)
				if np.any(np.isnan(ytrue)):
					raise ValueError("Function to interpolate returned invalid value")
				err = np.std((ytrue-yinter).reshape(ytrue.shape[0],-1), 1)
				if any(err > errlim):
					# Not good enough, so accept improvement
					ip = interpolator(box, ytrue, *args, **kwargs)
					n  = nnew
				else: nok += 1
				errs[i] = err
				# update output box
				obox[0] = np.minimum(obox[0], np.min(ytrue.reshape(ytrue.shape[0],-1),1))
				obox[1] = np.maximum(obox[0], np.max(ytrue.reshape(ytrue.shape[0],-1),1))
			else: nok += 1
		if nok >= idim: break
	return ip if not return_obox else ip, np.array(obox)

class Interpolator:
	def __init__(self, box, y, *args, **kwargs):
		self.box, self.y = np.array(box), np.array(y)
		self.args, self.kwargs = args, kwargs

class ip_ndimage(Interpolator):
	def __call__(self, x):
		ix = ((x.T-self.box[0])/(self.box[1]-self.box[0])*(np.array(self.y.shape[1:])-1)).T
		return utils.interpol(self.y, ix, *self.args, **self.kwargs)

class ip_linear(Interpolator):
	# General linear interpolation. This does the same as ndimage interpolation
	# using order=1, but is about 3 times slower.
	def __init__(self, box, y, *args, **kwargs):
		y = np.asarray(y)
		self.box = np.array(box)
		self.n, self.npre = self.box.shape[1], y.ndim-self.box.shape[1]
		self.ys  = lin_derivs_forward(y, self.npre)
	def __call__(self, x):
		flatx = x.reshape(x.shape[0],-1)
		px = ((flatx.T-self.box[0])/(self.box[1]-self.box[0])*np.array(self.ys.shape[-self.n:])).T
		ix = (np.floor(px)).astype(int)
		ix = np.maximum(0,np.minimum(np.array(self.ys.shape[-self.n:])[:,None]-1,ix))
		fx = px-ix
		res = np.zeros(self.ys.shape[self.n:self.n+self.npre]+fx.shape[1:2])
		for i in range(2**self.n):
			I = np.unravel_index(i,(2,)*self.n)
			res += self.ys[I][(slice(None),)*self.npre+tuple(ix)]*np.prod(fx**(np.array(I)[:,None]),0)
		return res.reshape(res.shape[:-1]+x.shape[1:])

class ip_grad(Interpolator):
	def __init__(self, box, y, *args, **kwargs):
		y = np.asarray(y)
		self.box = np.array(box)
		self.n, self.npre = self.box.shape[1], y.ndim-self.box.shape[1]
		self.dy  = grad_forward(y, self.npre)
		self.y   = y[(slice(None,-1),)*y.nsim]
	def __call__(self, x):
		flatx = x.reshape(x.shape[0],-1)
		px = ((flatx.T-self.box[0])/(self.box[1]-self.box[0])*np.array(self.ys.shape[-self.n:])).T
		ix = (np.floor(px)).astype(int)
		ix = np.maximum(0,np.minimum(np.array(self.ys.shape[-self.n:])[:,None]-1,ix))
		fx = px-ix
		res = self.y[tuple(ix)]
		for i in range(self.n):
			res += self.dy[tuple(ix)]*fx[i]
		return res.reshape(res.shape[:-1]+x.shape[1:])

def lin_derivs_forward(y, npre=0):
	"""Given an array y with npre leading dimensions and n following dimensions,
	compute all combinations of the 0th and 1st derivatives along the n last
	dimensions, returning an array of shape (2,)*n+(:,)*npre+(:-1,)*n. Derivatives
	are computed using forward difference."""
	y        = np.asfarray(y)
	nin      = y.ndim-npre
	ys = np.zeros((2,)*nin+y.shape)
	ys[(0,)*nin] = y
	for i in range(nin):
		whole,start,end = slice(None,None,None), slice(0,-1,None), slice(1,None,None)
		target = (whole,)*(i)+(1,)+(0,)*(nin-i-1)
		source = (whole,)*(i)+(0,)+(0,)*(nin-i-1)
		cells1 = (whole,)*(npre+i)+(start,)+(whole,)*(nin-i-1)
		cells2 = (whole,)*(npre+i)+(end,)  +(whole,)*(nin-i-1)
		ys[target+cells1] = ys[source+cells2]-ys[source+cells1]
	ys = ys[(slice(None),)*(nin+npre)+(slice(0,-1),)*nin]
	return ys

def grad_forward(y, npre=0):
	"""Given an array y with npre leading dimensions and n following dimensions,
	the gradient along the n last dimensions, returning an array of shape (n,)+y.shape.
	Derivatives are computed using forward difference."""
	y        = np.asfarray(y)
	nin      = y.ndim-npre
	dy       = np.zeros((nin,)+y.shape)
	for i in range(nin):
		whole,start,end = slice(None,None,None), slice(0,-1,None), slice(1,None,None)
		source = (whole,)*i+(0,)+(0,)*(nin-i-1)
		cells1 = (whole,)*(npre+i)+(start,)+(whole,)*(nin-i-1)
		cells2 = (whole,)*(npre+i)+(end,)  +(whole,)*(nin-i-1)
		dy[i][cells1] = y[source+cells2]-y[source+cells1]
	return dy[(slice(None),)+(slice(None,-1),)*(dy.ndim-1)]

def get_core(dtype):
	if dtype == np.float32:   return fortran_32.fortran
	elif dtype == np.float64: return fortran_64.fortran

def spline_filter(data, order=3, border="cyclic", ndim=None, trans=False):
	data = np.array(data)
	core = get_core(data.dtype)
	iborder = {"zero":0, "nearest":1, "cyclic":2, "mirror":3}[border]
	if ndim is None: ndim = data.ndim
	for axis in range(ndim)[::-1 if trans else 1]:
		core.spline_filter1d(data.reshape(-1), data.shape, axis, order, iborder, trans)
	return data

# idata[{dims},{isub}], points[ndim,{osub}], odata[{osub},{isub}]
# This differs from scipy.map_coordinates, which has idata[{dims}], points[ndim,{osub}],
# odata[{osub}]. But they are compatible when there are no isub dimensions.
# The keywords differ, though.
def map_coordinates(idata, points, odata=None, mode="spline", order=3, border="cyclic", trans=False):
	"""An alternative implementation of scipy.ndimage.map_coordinates. It is slightly
	slower (20-30%), but more general. Basic usage is
	 odata[{pdims},{isub}] = map_coordinates(idata[{dims},{isub}], points[ndim,{pdims}])
	where {foo} means a (possibly empty) shape. For example, if idata has shape (10,20)
	and points has shape (2,100), then the result will have shape (100,), and if
	idata has shape (10,20,30,40) and points has shape (3,1,2,3,4), then the result
	will have shape (1,2,3,4,40). Except for the presence of {isub}, this is the same
	as how map_coordinates works.

	It is also possible to pass the output array as an argument (odata), which must
	have the same data type as idata in that case.

	The function differs from ndimage in the meaning of the optional arguments.
	mode specifies the interpolation scheme to use: "conv", "spline" or "lanczos".
	"conv" is polynomial convolution, which is commonly used in image processing.
	"spline" is spline interpolation, which is what ndimage uses.
	"lanczos" convolutes with a lanczos kernerl, which approximates the optimal
	sinc kernel. This is slow, and the quality is not much better than splin.

	order specifies the interpolation order, its exact meaning differs based on
	mode.

	border specifies the handling of boundary conditions. It can be "zero",
	"nearest", "cyclic" or "mirror". "mirror" corresponds to ndimage's
	"reflect". The others do not match ndimage due to ndimage's inconsistent
	treatment of boundary conditions in spline_filter vs. map_coordiantes.

	trans specifies whether to perform the transpose operation or not.
	The interpolation performed by map_coordinates is a linear operation,
	and can hence be expressed as out = A*data, where A is a matrix.
	If trans is true, then what will instead be performed is data = A.T*in.
	For this to work, the odata argument must be specified."""

	imode   = {"conv":0, "spline":1, "lanczos":2}[mode]
	iborder = {"zero":0, "nearest":1, "cyclic":2, "mirror":3}[border]
	idata   = np.asarray(idata)
	points  = np.asarray(points)
	core    = get_core(idata.dtype)
	ndim    = points.shape[0]
	dpre,dpost= idata.shape[:ndim], idata.shape[ndim:]
	if not trans:
		if odata is None:
			odata = np.empty(points.shape[1:]+dpost,dtype=idata.dtype)
		if mode == "spline": idata = spline_filter(idata, order=order, border=border, ndim=ndim, trans=False)
		core.interpol(
			idata.reshape(np.product(dpre),np.product(dpost)).T, dpre,
			odata.reshape(np.product(points.shape[1:]),np.product(dpost)).T,
			points.reshape(ndim, -1).T,
			imode, order, iborder, False)
		return odata
	else:
		# We cannot infer the shape of idata from odata and points. So both
		# idata and odata must be specified in this case.
		core.interpol(
			idata.reshape(np.product(dpre),np.product(dpost)).T, dpre,
			odata.reshape(np.product(points.shape[1:]),np.product(dpost)).T,
			points.reshape(ndim,-1).T,
			imode, order, iborder, True)
		if mode == "spline": idata[:] = spline_filter(idata, order=order, border=border, ndim=ndim, trans=True)
		return idata
