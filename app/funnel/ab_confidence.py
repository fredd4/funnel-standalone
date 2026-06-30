from math import exp

def erfcc(x):
    """Complementary error function."""
    z = abs(x)
    t = 1. / (1. + 0.5*z)
    r = t * exp(-z*z-1.26551223+t*(1.00002368+t*(.37409196+
        t*(.09678418+t*(-.18628806+t*(.27886807+
        t*(-1.13520398+t*(1.48851587+t*(-.82215223+
        t*.17087277)))))))))
    if (x >= 0.):
        return r
    else:
        return 2. - r

def ncdf(x):
    """ This works exactly the same as scipy.stats.norm.cdf

    But does not need more requirements """

    return 1. - 0.5*erfcc(x/(2**0.5))

class ABConfidence(object):
    """Class which calculates confidence rate and significance of AB tests results.
    Based on: http://www.abtester.com/calculator/"""
    def __init__(self, control_size, control_conversions):
        self.control_sample_size, self.control_conversions = control_size, control_conversions
        self.control_converstion_rate = self.__conversion_rate((control_size, control_conversions))
        self.data_sets = []

    def __conversion_rate(self, dataset):
        return float(dataset[1])/dataset[0]

    def __confidence(self, dataset):
        z = self.__zscore(dataset)
        return ncdf(z)

    def __significance(self, dataset):
        a = 3.84145882689
        cr = self.__conversion_rate(dataset)
        return (dataset[0] > (1-cr)*a/(0.0225*cr), int((1-cr)*a/(0.0225*cr)))

    def __standard_error(self, dataset):
        cr = self.__conversion_rate(dataset)
        return (cr*(1-cr))/dataset[0]

    def __zscore(self, dataset):
        cr = self.__conversion_rate(dataset)
        z = cr - self.control_converstion_rate
        s = self.__standard_error(dataset) + (self.control_converstion_rate*(1-self.control_converstion_rate))/self.control_sample_size
        return z/s**(0.5)

    def add_data_set(self, size, conversions):
        dataset = (size, conversions)
        self.data_sets.append(dataset + (self.__conversion_rate(dataset), self.__confidence(dataset)) + self.__significance(dataset))

    def __getitem__(self, key):
        """Returns a tuple: (sample size, sample conversions, conversion rate, confidence, significance achieved, min. significant dataset size)"""
        return self.data_sets[key]

    def __iter__(self):
        return (x for x in self.data_sets)

    def __delitem__(self, key):
        del self.data_sets[key]

    def __str__(self):
        a = "Sample" + "Sample size".rjust(15) + "Conversions".rjust(15) + "%".rjust(7) + "Confidence".rjust(15) + "Significance".rjust(20) + '\n'
        a += ("C".rjust(6) + str(self.control_sample_size).rjust(15) + str(self.control_conversions).rjust(15) + ("%.2f%%" % (self.control_converstion_rate*100)).rjust(7) + '\n')
        b = 0
        for x in self.data_sets:
            a += (str(b).rjust(6) + str(x[0]).rjust(15) + str(x[1]).rjust(15) + ("%.2f%%" % (x[2]*100)).rjust(7) + ("%.2f%%" % (x[3]*100)).rjust(15) + (str(x[4]) + ", " + str(x[5])).rjust(20) + '\n')
            b += 1
        return a

